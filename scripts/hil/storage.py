"""Private-by-default storage for read-only HIL preflight sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any

from .api import HAAPIError


def write_run(
    root: Path, run_name: str, environment: dict[str, Any], session: dict[str, Any]
) -> Path:
    """Create an isolated run directory; write only curated JSON with safe perms."""
    root = root.expanduser()
    if root.is_symlink():
        raise HAAPIError("Output root must not be a symlink")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise HAAPIError("Invalid output directory")
        run_dir = root / run_name
        run_dir.mkdir(mode=0o700, exist_ok=False)
        for name, value in (
            ("environment.json", environment),
            ("session.json", session),
        ):
            file_path = run_dir / name
            data = (
                json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
            ).encode("utf-8")
            fd = os.open(file_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
    except (OSError, ValueError) as ex:
        raise HAAPIError("Unable to create private HIL result files") from ex
    return run_dir


def write_scenario_progress(
    run_dir: Path,
    run_id: str,
    scenario_id: str,
    device_fingerprint: str,
    steps: list[dict[str, Any]],
) -> None:
    """Atomically persist private scenario results and a restart checkpoint."""
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise HAAPIError("Invalid private scenario directory")
    payload = {
        "schema_version": 2,
        "run_id": run_id,
        "scenario_id": scenario_id,
        "device_fingerprint": device_fingerprint,
        "qualification_status": "inconclusive",
        "steps": steps,
    }
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")
    created: list[Path] = []
    try:
        for name in ("scenarios.json", "checkpoint.json"):
            destination = run_dir / name
            if destination.is_symlink():
                raise OSError("scenario path is a symlink")
            temporary = run_dir / f".{name}.{secrets.token_hex(8)}.tmp"
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created.append(temporary)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            created.remove(temporary)
    except (OSError, TypeError, ValueError) as ex:
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise HAAPIError("Unable to persist private scenario progress") from ex


def load_scenario_checkpoint(run_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Load a private, versioned checkpoint without following symlinks."""
    run_dir = run_dir.expanduser()
    absolute = Path(os.path.abspath(run_dir))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise HAAPIError("Checkpoint path must not contain symlinks")
    if not run_dir.is_dir():
        raise HAAPIError("Checkpoint directory does not exist")
    try:
        directory_mode = stat.S_IMODE(run_dir.stat().st_mode)
        if directory_mode & 0o077:
            raise HAAPIError("Checkpoint directory permissions are too broad")
        checkpoint_path = run_dir / "checkpoint.json"
        scenarios_path = run_dir / "scenarios.json"
        session_path = run_dir / "session.json"
        for path in (checkpoint_path, scenarios_path, session_path):
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_size > 1024 * 1024
            ):
                raise HAAPIError("Checkpoint files are not private regular files")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        scenarios = json.loads(scenarios_path.read_text(encoding="utf-8"))
        session = json.loads(session_path.read_text(encoding="utf-8"))
    except HAAPIError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as ex:
        raise HAAPIError("Unable to read private scenario checkpoint") from ex

    expected = {
        "schema_version",
        "run_id",
        "scenario_id",
        "device_fingerprint",
        "qualification_status",
        "steps",
    }
    steps = checkpoint.get("steps") if isinstance(checkpoint, dict) else None
    if (
        not isinstance(checkpoint, dict)
        or set(checkpoint) != expected
        or checkpoint.get("schema_version") != 2
        or not isinstance(checkpoint.get("run_id"), str)
        or len(checkpoint["run_id"]) != 16
        or any(char not in "0123456789abcdef" for char in checkpoint["run_id"])
        or not isinstance(checkpoint.get("scenario_id"), str)
        or not isinstance(checkpoint.get("device_fingerprint"), str)
        or len(checkpoint["device_fingerprint"]) != 64
        or any(
            char not in "0123456789abcdef" for char in checkpoint["device_fingerprint"]
        )
        or checkpoint.get("qualification_status") != "inconclusive"
        or not isinstance(steps, list)
        or len(steps) > 128
        or not isinstance(session, dict)
        or session.get("run_id") != checkpoint.get("run_id")
        or scenarios != checkpoint
    ):
        raise HAAPIError("Checkpoint schema or run identity is invalid")
    if any(
        not isinstance(step, dict)
        or not isinstance(step.get("step_id"), str)
        or not isinstance(step.get("outcome"), str)
        or step["outcome"]
        not in {"pass", "fail", "inconclusive", "skipped", "not_applicable"}
        for step in steps
    ):
        raise HAAPIError("Checkpoint contains invalid scenario steps")
    return run_dir, checkpoint
