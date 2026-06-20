"""Atomic JSON persistence for trading_state.json."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_STATE_FILE = "./trading_state.json"


def load_persisted_states(path: str = DEFAULT_STATE_FILE) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}

    with open(path, encoding="utf-8") as handle:
        raw = handle.read().strip()
        if not raw:
            return {}
        payload = json.loads(raw)

    if not isinstance(payload, dict):
        return {}
    return payload


def save_persisted_states(states: dict[str, Any], path: str = DEFAULT_STATE_FILE) -> None:
    """Write state atomically: backup prior file, temp write, fsync, replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    directory = str(target.parent) if str(target.parent) not in {"", "."} else "."

    if target.exists() and target.stat().st_size > 0:
        backup = target.with_name(f"{target.name}.bak")
        shutil.copy2(target, backup)

    fd, temp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(states, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
