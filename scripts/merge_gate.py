#!/usr/bin/env python3
"""Enforce pull request policy and wait for checks tied to exact PR revisions."""

from __future__ import annotations

import base64
from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from scripts.promotion_metadata import (
    INTEGRATION_MANIFEST,
    MetadataError,
    validate_gate_release_metadata,
    validate_release_file_changes,
)

REQUIRED_CHECKS = (
    "Python, tests, and release validation",
    "HACS validation",
    "Home Assistant Hassfest",
    "Validate title",
    "Review dependency changes",
    "Analyze Python",
)
REQUIRED_WORKFLOWS = {
    "Python, tests, and release validation": "CI",
    "HACS validation": "HACS",
    "Home Assistant Hassfest": "Hassfest",
    "Validate title": "Conventional pull request title",
    "Review dependency changes": "Dependency review",
    "Analyze Python": "CodeQL",
    "HIL qualification (opt-in)": "HIL qualification",
}
WORKFLOW_PATHS = {
    "CI": ".github/workflows/ci.yml",
    "HACS": ".github/workflows/hacs.yml",
    "Hassfest": ".github/workflows/hassfest.yml",
    "Conventional pull request title": ".github/workflows/conventional-commits.yml",
    "Dependency review": ".github/workflows/dependency-review.yml",
    "CodeQL": ".github/workflows/codeql.yml",
    "HIL qualification": ".github/workflows/hil-qualification.yml",
}
REQUIRED_EVIDENCE_IDS = {
    "Python, tests, and release validation": ("ci", "quality"),
    "HACS validation": ("hacs", "validate"),
    "Home Assistant Hassfest": ("hassfest", "validate"),
    "Validate title": ("title", "validate"),
    "Review dependency changes": ("dependencies", "review"),
    "Analyze Python": ("codeql", "analyze"),
    "HIL qualification (opt-in)": ("hil", "qualification"),
}
ACCEPTED_CONCLUSIONS = {"success", "neutral", "skipped"}
POLL_INTERVAL_SECONDS = 15
TIMEOUT_SECONDS = 30 * 60
MISSING_CHECKS_GRACE_SECONDS = 10 * 60


class GateError(RuntimeError):
    """A policy violation or incomplete required-check set."""


class GitHubApi(Protocol):
    """Small API surface used by the gate and its focused tests."""

    def pull_request(self, number: int) -> dict[str, Any]: ...

    def pull_request_files(self, number: int) -> list[dict[str, Any]]: ...

    def compare(self, base: str, head: str) -> dict[str, Any]: ...

    def content(
        self, path: str, ref: str, repository_full_name: str | None = None
    ) -> str: ...

    def check_runs(self, ref: str) -> list[dict[str, Any]]: ...

    def workflow_run(self, run_id: int) -> dict[str, Any]: ...

    def workflow(self, workflow_id: int) -> dict[str, Any]: ...

    def workflow_run_attempt_jobs(
        self, run_id: int, attempt: int
    ) -> list[dict[str, Any]]: ...


class RestGitHubApi:
    """GitHub REST client using only Python's standard library."""

    def __init__(self, owner: str, repo: str, token: str, api_url: str) -> None:
        if not token:
            raise GateError("GITHUB_TOKEN is required to inspect PR checks.")
        self.owner = owner
        self.repo = repo
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.repository_path = f"/repos/{quote(owner)}/{quote(repo)}"

    def _request(self, path: str) -> tuple[Any, dict[str, str]]:
        url = f"{self.api_url}{path}"
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "home-assistant-allpowers-ble-merge-gate",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read()), dict(response.headers.items())
        except (
            HTTPError,
            URLError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise GateError(f"GitHub API request failed for {path}: {error}") from error

    def _get(self, path: str, **query: str | int) -> Any:
        suffix = f"?{urlencode(query)}" if query else ""
        return self._request(f"{path}{suffix}")[0]

    def _all_pages(self, path: str, key: str, **query: str | int) -> list[Any]:
        page = 1
        results: list[Any] = []
        while True:
            payload = self._get(path, per_page=100, page=page, **query)
            if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
                raise GateError(f"Unexpected GitHub API response while reading {path}.")
            batch = payload[key]
            if any(not isinstance(item, dict) for item in batch):
                raise GateError(f"Malformed GitHub API entries while reading {path}.")
            results.extend(batch)
            if len(batch) < 100:
                return results
            page += 1

    def pull_request(self, number: int) -> dict[str, Any]:
        value = self._get(f"{self.repository_path}/pulls/{number}")
        if not isinstance(value, dict):
            raise GateError("GitHub returned an invalid pull request object.")
        return value

    def pull_request_files(self, number: int) -> list[dict[str, Any]]:
        page = 1
        files: list[dict[str, Any]] = []
        while True:
            values = self._get(
                f"{self.repository_path}/pulls/{number}/files",
                per_page=100,
                page=page,
            )
            if not isinstance(values, list) or any(
                not isinstance(value, dict) for value in values
            ):
                raise GateError("GitHub returned an invalid pull request file list.")
            files.extend(values)
            if len(values) < 100:
                return files
            page += 1

    def compare(self, base: str, head: str) -> dict[str, Any]:
        value = self._get(
            f"{self.repository_path}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
        )
        if not isinstance(value, dict):
            raise GateError("GitHub returned an invalid commit comparison.")
        return value

    def content(
        self, path: str, ref: str, repository_full_name: str | None = None
    ) -> str:
        repository_path = self.repository_path
        if repository_full_name is not None:
            parts = repository_full_name.split("/")
            if len(parts) != 2 or not all(parts):
                raise GateError("GitHub returned an invalid head repository identity.")
            repository_path = f"/repos/{quote(parts[0])}/{quote(parts[1])}"
        value = self._get(
            f"{repository_path}/contents/{quote(path, safe='/')}", ref=ref
        )
        if not isinstance(value, dict) or value.get("type") != "file":
            raise GateError(f"Expected a file at {path} for {ref}.")
        encoded = value.get("content")
        if value.get("encoding") != "base64" or not isinstance(encoded, str):
            raise GateError(
                f"GitHub returned missing or unsupported content encoding for {path} at {ref}."
            )
        try:
            # The Contents API may line-wrap base64, but invalid characters must
            # not be silently discarded (notably for changelog history checks).
            compact = encoded.replace("\r", "").replace("\n", "")
            return base64.b64decode(compact, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise GateError(f"Could not decode {path} at {ref}: {error}") from error

    def check_runs(self, ref: str) -> list[dict[str, Any]]:
        return self._all_pages(
            f"{self.repository_path}/commits/{quote(ref, safe='')}/check-runs",
            "check_runs",
            filter="all",
        )

    def workflow_run(self, run_id: int) -> dict[str, Any]:
        value = self._get(f"{self.repository_path}/actions/runs/{run_id}")
        if not isinstance(value, dict):
            raise GateError("GitHub returned an invalid Actions workflow run.")
        return value

    def workflow(self, workflow_id: int) -> dict[str, Any]:
        value = self._get(f"{self.repository_path}/actions/workflows/{workflow_id}")
        if not isinstance(value, dict):
            raise GateError("GitHub returned an invalid Actions workflow.")
        return value

    def workflow_run_attempt_jobs(
        self, run_id: int, attempt: int
    ) -> list[dict[str, Any]]:
        if run_id <= 0 or attempt <= 0:
            raise GateError("Actions run ID and attempt must be positive integers.")
        return self._all_pages(
            f"{self.repository_path}/actions/runs/{run_id}/attempts/{attempt}/jobs",
            "jobs",
        )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _workflow_run_id(details_url: Any, owner: str, repo: str) -> int | None:
    if not isinstance(details_url, str):
        return None
    parsed = urlparse(details_url)
    expected_prefix = f"/{owner}/{repo}/actions/runs/"
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        return None
    if not parsed.path.lower().startswith(expected_prefix.lower()):
        return None
    suffix = parsed.path[len(expected_prefix) :]
    match = re.match(r"^(\d+)(?:/|$)", suffix)
    try:
        return int(match.group(1)) if match else None
    except ValueError:
        return None


def _current_workflow_run(
    api: GitHubApi,
    run: dict[str, Any],
    owner: str,
    repo: str,
    number: int,
    base_sha: str,
    head_sha: str,
    tested_sha: str,
    check_name: str,
    workflow_cache: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    workflow_name = REQUIRED_WORKFLOWS.get(check_name)
    evidence_ids = REQUIRED_EVIDENCE_IDS.get(check_name)
    if workflow_name is None or evidence_ids is None:
        return None
    run_id = _workflow_run_id(run.get("details_url"), owner, repo)
    if run_id is None:
        return None
    workflow_run = api.workflow_run(run_id)
    if not isinstance(workflow_run, dict):
        raise GateError("GitHub returned a malformed Actions workflow run.")
    if workflow_run.get("id") != run_id:
        return None
    workflow_id = workflow_run.get("workflow_id")
    if isinstance(workflow_id, bool) or not isinstance(workflow_id, int):
        return None
    workflow = workflow_cache.get(workflow_id)
    if workflow is None:
        workflow = api.workflow(workflow_id)
        if not isinstance(workflow, dict):
            raise GateError("GitHub returned a malformed Actions workflow.")
        workflow_cache[workflow_id] = workflow
    expected = (
        f"evidence w={evidence_ids[0]} j={evidence_ids[1]} pr={number} "
        f"r={owner}/{repo} b={base_sha} h={head_sha} s={tested_sha}"
    )
    if not (
        workflow_run.get("event") == "pull_request"
        and workflow_run.get("display_title") == expected
        and workflow.get("name") == workflow_name
        and workflow.get("path") == WORKFLOW_PATHS.get(workflow_name)
        and workflow_run.get("head_sha") == head_sha
    ):
        return None
    attempt = workflow_run.get("run_attempt")
    run_number = workflow_run.get("run_number")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise GateError(
            f"Applicable workflow run {run_id} has missing or invalid run_attempt."
        )
    if (
        isinstance(run_number, bool)
        or not isinstance(run_number, int)
        or run_number <= 0
    ):
        raise GateError(
            f"Applicable workflow run {run_id} has missing or invalid run_number."
        )
    status = workflow_run.get("status")
    if not isinstance(status, str) or status not in {
        "queued",
        "in_progress",
        "completed",
        "waiting",
        "pending",
        "requested",
    }:
        raise GateError(
            f"Applicable workflow run {run_id} has unknown status {status!r}."
        )
    return workflow_run


def _check_run_id_from_url(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"/check-runs/(\d+)$", urlparse(value).path)
    return int(match.group(1)) if match else None


def _job_for_current_attempt(
    api: GitHubApi,
    owner: str,
    repo: str,
    run_id: int,
    attempt: int,
    check_name: str,
    check_runs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    jobs = api.workflow_run_attempt_jobs(run_id, attempt)
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise GateError(
            f"GitHub returned malformed jobs for workflow run {run_id} attempt {attempt}."
        )
    candidates = {
        check.get("id")
        for check in check_runs
        if _workflow_run_id(check.get("details_url"), owner, repo) == run_id
    }
    matching = [job for job in jobs if job.get("name") == check_name]
    if len(matching) > 1:
        raise GateError(
            f"Workflow run {run_id} attempt {attempt} has multiple {check_name!r} jobs."
        )
    if not matching:
        return None
    job = matching[0]
    job_id = job.get("id")
    job_run_id = job.get("run_id")
    check_run_id = _check_run_id_from_url(job.get("check_run_url"))
    if (
        isinstance(job_id, bool)
        or not isinstance(job_id, int)
        or job_id <= 0
        or isinstance(job_run_id, bool)
        or not isinstance(job_run_id, int)
        or job_run_id != run_id
        or isinstance(job.get("run_attempt"), bool)
        or not isinstance(job.get("run_attempt"), int)
        or job.get("run_attempt") != attempt
        or check_run_id is None
        or check_run_id not in candidates
    ):
        return None
    return job


def _validate_release_metadata(
    api: GitHubApi,
    base_sha: str,
    head_sha: str,
    allow_newer: bool,
    head_repository: str | None = None,
) -> None:
    def json_at(path: str, ref: str, repository: str | None = None) -> Any:
        try:
            return json.loads(api.content(path, ref, repository))
        except json.JSONDecodeError as error:
            raise GateError(f"Invalid JSON in {path} at {ref}.") from error

    validate_gate_release_metadata(
        base_integration=json_at(
            "custom_components/allpowers_ble/manifest.json", base_sha
        ),
        head_integration=json_at(
            "custom_components/allpowers_ble/manifest.json", head_sha, head_repository
        ),
        base_release=json_at(".release-please-manifest.json", base_sha),
        head_release=json_at(
            ".release-please-manifest.json", head_sha, head_repository
        ),
        base_changelog=api.content("CHANGELOG.md", base_sha),
        head_changelog=api.content("CHANGELOG.md", head_sha, head_repository),
        allow_newer=allow_newer,
    )


def _check_policy(
    api: GitHubApi, event: dict[str, Any], owner: str, repo: str
) -> tuple[dict[str, Any], list[dict[str, Any]], bool, bool]:
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        raise GateError("This workflow only supports pull_request events.")
    base = _mapping(pr.get("base"))
    head = _mapping(pr.get("head"))
    base_ref = str(base.get("ref") or "")
    head_ref = str(head.get("ref") or "")
    head_sha = str(head.get("sha") or "")
    base_sha = str(base.get("sha") or "")
    number = pr.get("number")
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or not head_sha
        or not base_sha
    ):
        raise GateError("PR event is missing its number or immutable base/head SHA.")

    author = _mapping(pr.get("user"))
    author_login = str(author.get("login") or "")
    author_type = str(author.get("type") or "")
    is_hotfix = head_ref.startswith("hotfix/")
    is_release_please = head_ref.startswith("release-please--")
    is_approved_bot = author_type == "Bot" and author_login in {
        "github-actions[bot]",
        "release-please[bot]",
    }

    if (
        base_ref == "main"
        and head_ref != "devel"
        and not is_hotfix
        and not is_release_please
    ):
        raise GateError(
            "PRs targeting main must come from 'devel', 'hotfix/*', or "
            f"'release-please--*'. Current source: '{head_ref}'."
        )
    if base_ref == "main" and is_release_please and not is_approved_bot:
        raise GateError(
            f"PRs from 'release-please--*' to main must be authored by an approved bot. "
            f"Current author: '{author_login}' ({author_type or 'unknown'})."
        )
    if base_ref == "main" and is_hotfix:
        comparison = api.compare("main", head_sha)
        if comparison.get("status") not in {"ahead", "identical"}:
            raise GateError(
                f"Hotfix branch '{head_ref}' must be based on 'main'. Create hotfix branches from 'main'."
            )

    files = api.pull_request_files(number)
    if not isinstance(files, list) or any(
        not isinstance(file, dict)
        or not isinstance(file.get("filename"), str)
        or file.get("previous_filename") is not None
        and not isinstance(file.get("previous_filename"), str)
        for file in files
    ):
        raise GateError("GitHub returned a malformed pull request file list.")
    repository_name = f"{owner}/{repo}"
    base_repo = _mapping(base.get("repo")).get("full_name")
    head_repo = _mapping(head.get("repo")).get("full_name")
    is_sync = (
        base_ref == "devel"
        and head_ref == "main"
        and base_repo == repository_name
        and head_repo == repository_name
    )
    is_promotion = base_ref == "main" and head_ref == "devel"
    release_please_update = base_ref == "main" and is_release_please
    changed_paths = {
        path
        for file in files
        for path in (file.get("filename"), file.get("previous_filename"))
        if isinstance(path, str)
    }
    manifest_deleted_or_renamed = any(
        file.get("filename") == INTEGRATION_MANIFEST
        and file.get("status") in {"removed", "renamed"}
        or file.get("previous_filename") == INTEGRATION_MANIFEST
        for file in files
    )
    if manifest_deleted_or_renamed:
        changed_paths.add(f"{INTEGRATION_MANIFEST} (removed)")
    try:
        validate_release_file_changes(
            changed_paths,
            base_ref,
            head_ref,
            same_repository_sync=is_sync,
        )
    except MetadataError as error:
        raise GateError(str(error)) from error

    manifest_changed = INTEGRATION_MANIFEST in changed_paths
    if manifest_changed and not (is_sync or is_promotion or release_please_update):
        if not isinstance(head_repo, str) or not head_repo:
            raise GateError(
                "Head repository identity is required to validate manifest changes."
            )

    if is_sync or is_promotion or release_please_update or manifest_changed:
        allow_newer = is_sync or release_please_update
        try:
            _validate_release_metadata(
                api,
                base_sha,
                head_sha,
                allow_newer=allow_newer,
                head_repository=head_repo if head_repo != repository_name else None,
            )
        except MetadataError as error:
            raise GateError(f"Release metadata validation failed: {error}") from error
        current = api.pull_request(number)
        current_base = _mapping(current.get("base"))
        current_head = _mapping(current.get("head"))
        identity_changed = (
            _mapping(current_base.get("repo")).get("full_name") != base_repo
            or _mapping(current_head.get("repo")).get("full_name") != head_repo
        )
        if (
            current_base.get("ref") != base_ref
            or current_head.get("ref") != head_ref
            or current_base.get("sha") != base_sha
            or current_head.get("sha") != head_sha
            or identity_changed
        ):
            raise GateError(
                "PR refs moved during release metadata validation; rerun the merge gate."
            )

    return pr, files, is_sync, is_promotion


def run_gate(
    event: dict[str, Any],
    api: GitHubApi,
    owner: str,
    repo: str,
    *,
    tested_sha: str | None = None,
    require_hil_stable: bool = False,
    log: Callable[[str], None] = print,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Validate policy and wait until each required check passes for this PR."""
    pr, _files, _is_sync, _is_promotion = _check_policy(api, event, owner, repo)
    if pr.get("draft") is True:
        log("Draft PR detected. Skipping strict gate until ready for review.")
        return

    base = _mapping(pr.get("base"))
    head = _mapping(pr.get("head"))
    number = int(pr["number"])
    base_ref = str(base.get("ref") or "")
    base_sha = str(base.get("sha") or "")
    head_sha = str(head.get("sha") or "")
    tested_sha = tested_sha or str(pr.get("merge_commit_sha") or "")
    if not tested_sha:
        raise GateError("Event is missing the immutable pull request merge revision.")
    title_was_edited = "title" in _mapping(event.get("changes"))
    required = list(REQUIRED_CHECKS)
    if base_ref == "main" and require_hil_stable:
        required.append("HIL qualification (opt-in)")
        log("REQUIRE_HIL_STABLE_GATE=true; enforcing HIL qualification check.")

    started = now()
    while True:
        elapsed = now() - started
        if elapsed > TIMEOUT_SECONDS:
            raise GateError("Timeout waiting for required checks to complete.")

        runs = api.check_runs(head_sha)
        if not isinstance(runs, list):
            raise GateError("GitHub returned a malformed check-run list.")
        workflow_cache: dict[int, dict[str, Any]] = {}
        workflow_runs: dict[int, dict[str, Any]] = {}
        applicable: dict[str, dict[int, list[dict[str, Any]]]] = {}
        for candidate in runs:
            if not isinstance(candidate, dict):
                raise GateError("GitHub returned a malformed check-run entry.")
            name = candidate.get("name")
            if (
                candidate.get("head_sha") != head_sha
                or not isinstance(name, str)
                or name not in REQUIRED_WORKFLOWS
            ):
                continue
            candidate_id = candidate.get("id")
            if (
                isinstance(candidate_id, bool)
                or not isinstance(candidate_id, int)
                or candidate_id <= 0
            ):
                raise GateError(f"Check run {name!r} has a missing or invalid ID.")
            run_id = _workflow_run_id(candidate.get("details_url"), owner, repo)
            if run_id is None:
                continue
            if run_id not in workflow_runs:
                revision = _current_workflow_run(
                    api,
                    candidate,
                    owner,
                    repo,
                    number,
                    base_sha,
                    head_sha,
                    tested_sha,
                    name,
                    workflow_cache,
                )
                if revision is None:
                    continue
                workflow_runs[run_id] = revision
            applicable.setdefault(name, {}).setdefault(run_id, []).append(candidate)

        latest: dict[str, dict[str, Any]] = {}
        selected_attempts: dict[str, tuple[int, int]] = {}
        selected_runs: dict[str, dict[str, Any]] = {}
        for name, runs_by_id in applicable.items():
            selected_run_id = max(
                runs_by_id,
                key=lambda run_id: (
                    workflow_runs[run_id]["run_number"],
                    run_id,
                ),
            )
            run_metadata = workflow_runs[selected_run_id]
            attempt = run_metadata["run_attempt"]
            selected_attempts[name] = (selected_run_id, attempt)
            selected_runs[name] = run_metadata
            job = _job_for_current_attempt(
                api,
                owner,
                repo,
                selected_run_id,
                attempt,
                name,
                runs_by_id[selected_run_id],
            )
            if job is not None:
                latest[name] = job

        changed_attempts: list[str] = []
        for name, (run_id, attempt) in selected_attempts.items():
            current_run = api.workflow_run(run_id)
            if not isinstance(current_run, dict):
                raise GateError("GitHub returned a malformed Actions workflow run.")
            current_attempt = current_run.get("run_attempt")
            selected_run = selected_runs[name]
            if (
                isinstance(current_attempt, bool)
                or not isinstance(current_attempt, int)
                or current_attempt <= 0
                or current_attempt != attempt
                or current_run.get("id") != run_id
                or current_run.get("workflow_id") != selected_run.get("workflow_id")
                or current_run.get("head_sha") != head_sha
                or current_run.get("display_title") != selected_run.get("display_title")
            ):
                changed_attempts.append(
                    f"{name} (workflow run {run_id} attempt/evidence changed "
                    f"from {attempt} to {current_attempt!r})"
                )
        if changed_attempts:
            log("Pending: " + ", ".join(changed_attempts))
            sleep(POLL_INTERVAL_SECONDS)
            continue

        missing: list[str] = []
        pending: list[str] = []
        failed: list[str] = []
        for name in required:
            if name == "Validate title" and title_was_edited:
                continue
            check = latest.get(name)
            if check is None:
                selected = selected_runs.get(name)
                if selected is not None and selected.get("status") in {
                    "queued",
                    "in_progress",
                    "waiting",
                    "pending",
                    "requested",
                }:
                    pending.append(f"{name} (current attempt job not yet available)")
                else:
                    missing.append(name)
                continue
            status = check.get("status")
            if not isinstance(status, str) or status not in {
                "queued",
                "in_progress",
                "completed",
            }:
                raise GateError(
                    f"Required check {name} has malformed status {status!r}."
                )
            if status != "completed":
                pending.append(f"{name} ({status})")
            elif check.get("conclusion") not in ACCEPTED_CONCLUSIONS:
                failed.append(f"{name} ({check.get('conclusion') or 'unknown'})")

        log(f"PR #{number} base SHA: {base_sha}")
        log(f"PR #{number} head SHA: {head_sha}")
        if missing:
            log("Missing: " + ", ".join(missing))
        if pending:
            log("Pending: " + ", ".join(pending))
        if failed:
            log("Failed: " + ", ".join(failed))
            raise GateError("Required checks failed: " + "; ".join(failed))
        if missing and elapsed >= MISSING_CHECKS_GRACE_SECONDS:
            discovered = ", ".join(sorted(latest)) or "none"
            raise GateError(
                "Missing required checks after 10 minutes: "
                + ", ".join(missing)
                + f". Discovered checks: {discovered}. If checks were renamed, update REQUIRED_CHECKS in scripts/merge_gate.py."
            )
        if not missing and not pending:
            current = api.pull_request(number)
            current_base = _mapping(current.get("base"))
            current_head = _mapping(current.get("head"))
            if (
                current.get("title") != pr.get("title")
                or current_base.get("ref") != base_ref
                or current_head.get("ref") != str(head.get("ref") or "")
                or current_base.get("sha") != base_sha
                or current_head.get("sha") != head_sha
                or _mapping(current_base.get("repo")).get("full_name")
                != _mapping(base.get("repo")).get("full_name")
                or _mapping(current_head.get("repo")).get("full_name")
                != _mapping(head.get("repo")).get("full_name")
            ):
                raise GateError(
                    "PR title, refs, repository identity, or base/head commits changed while required checks were being validated; rerun the merge gate."
                )
            log("All required checks completed successfully.")
            return
        sleep(POLL_INTERVAL_SECONDS)


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not event_path or "/" not in repository:
        print("GITHUB_EVENT_PATH and GITHUB_REPOSITORY are required.", file=sys.stderr)
        return 2
    owner, repo = repository.split("/", 1)
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        if not isinstance(event, dict):
            raise GateError("GitHub event payload must be a JSON object.")
        api = RestGitHubApi(
            owner,
            repo,
            os.environ.get("GITHUB_TOKEN", ""),
            os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )
        run_gate(
            event,
            api,
            owner,
            repo,
            tested_sha=os.environ.get("GITHUB_SHA", ""),
            require_hil_stable=os.environ.get(
                "REQUIRE_HIL_STABLE_GATE", "false"
            ).lower()
            == "true",
        )
    except (OSError, json.JSONDecodeError, GateError, MetadataError) as error:
        print(f"Merge gate failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
