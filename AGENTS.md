# AGENTS.md — Repository Agent Instructions

These instructions apply to AI coding agents. Human contributors follow
`CONTRIBUTING.md`; commit signing remains optional for them.

## 1. General rules

- Read the assigned issue, including discussion and acceptance criteria,
  `CONTRIBUTING.md`, `.github/PULL_REQUEST_TEMPLATE.md`, and relevant project
  documentation before working.
- Use English for repository-facing content, including documentation, commits,
  pull requests, and review discussions.
- Follow existing architecture and safety requirements. Keep changes focused
  and proportionate to this mostly single-developer project; avoid unrelated
  refactors, speculative abstractions, and unnecessary dependencies.

## 2. Branches and development workflow

- Fetch the latest `origin/devel` and create an isolated feature branch or
  worktree from it. Do not rely on local `devel` or commit directly to `devel`
  or `main`.
- Work from the assigned issue, check for existing pull requests, and implement
  only the requested scope with meaningful tests and applicable validation.
- Open a pull request targeting `devel` autonomously when the work is mature;
  no extra approval is needed to open it. Address review feedback on that same
  pull request. Never merge a pull request.
- Keep parallel work in separate worktrees and branches. Do not overwrite
  another contributor's uncommitted work.

For an explicitly requested urgent production hotfix only, branch from fresh
`origin/main` using `hotfix/*`, target `main`, and propagate the fix to `devel`
in a follow-up pull request.

## 3. Commits and pull requests

- Use Conventional Commits and sign every agent-created commit with a signature
  GitHub verifies. Do not bypass signing or pre-commit hooks (`--no-verify` is
  forbidden). Run `pre-commit run --all-files` before pushing, as required by
  `CONTRIBUTING.md`, and verify signatures before presenting the pull request
  as ready. If verified signing is unavailable, report the blocker; do not make
  unsigned commits.
- Use a Conventional Commits pull request title, target `devel` except for an
  authorized hotfix, and link the assigned issue.
- Preserve every heading and checkbox state in
  `.github/PULL_REQUEST_TEMPLATE.md`; do not check or uncheck its boxes unless
  explicitly instructed. Describe the change, rationale, actual validation,
  safety impact, and known limitations. Distinguish simulation from physical
  hardware evidence. Do not edit `CHANGELOG.md`; release tooling owns it.
- A mature pull request may be opened before remote CI finishes. Fix CI failures
  on the same pull request. Opening a pull request is not approval; passing CI
  alone does not prove correctness or authorize merging.

## 4. Code quality, BLE safety, and testing

- Test observable behavior and relevant success and failure paths, including
  malformed input, stale data, timeout, disconnect, cancellation, retry, and
  recovery where applicable. Use realistic protocol fixtures and validated
  traces where relevant; do not mock away the behavior under test.
- Preserve unknown protocol fields and bits. Require fresh, authoritative state
  from the current GATT session before writes. Keep protocol code independent of
  Home Assistant and BLE transport where practical.
- Keep read-only HIL workflows free of unapproved writes, keepalive changes, and
  output controls. Sanitize diagnostics and captures. Clearly distinguish
  simulations, observed physical behavior, and proven protocol behavior; do not
  attribute failures to unverified causes.
- Keep validation claims factual and scope proportional to risk. Do not claim
  tests or hardware validation that were not performed.

## 5. Independent PR review

- Read and evaluate all findings from an independent review. Fix material
  defects and coverage gaps in commits to the existing pull request, respond
  with a technical rationale when disagreeing, and rerun applicable checks.
- CI passing does not invalidate a substantive finding. Avoid unnecessary PR
  replacements and do not resolve threads without addressing their substance.
- The human maintainer decides whether to merge.

## 6. Post-merge issue maintenance

Only after the human maintainer merges, verify the actual destination-branch
merge and review each issue acceptance criterion against the merged code, tests,
documentation, and evidence. Mark an issue checkbox `[x]` only when its criterion
is demonstrably satisfied; preserve all other issue content and leave incomplete
or unverified criteria unchecked. Close an issue as completed only when its
entire scope is resolved. A PR merged into `devel` may not close an issue
automatically. Report remaining criteria directly to the maintainer.

Do not post issue comments unless explicitly instructed, including progress or
completion notes. This does not restrict necessary discussion on an active PR.
PR-template checkboxes stay as provided; issue acceptance checkboxes are updated
only after verified post-merge completion. A merged PR does not by itself mean
the issue is complete.
