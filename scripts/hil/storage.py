"""Private-by-default storage for read-only HIL preflight sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
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
    run_dir: Path, run_id: str, steps: list[dict[str, Any]]
) -> None:
    """Atomically persist private scenario results and a restart checkpoint."""
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise HAAPIError("Invalid private scenario directory")
    payload = {
        "schema_version": 1,
        "run_id": run_id,
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
