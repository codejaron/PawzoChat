# PawzoChat - Multi-platform LLM-powered chatbot
# Copyright (C) 2026  iwyxdxl
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Persistent Flask session-key management for headless deployments."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


_KEY_BYTES = 32


def _write_session_key(target: Path, key: bytes) -> None:
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp",
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_or_create_session_key(path: str | Path) -> bytes:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            key = target.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"session key cannot be read: {target}") from exc
        if len(key) != _KEY_BYTES:
            raise RuntimeError(f"session key has an invalid length: {target}")
        return key

    key = os.urandom(_KEY_BYTES)
    _write_session_key(target, key)
    return key


def rotate_session_key(path: str | Path) -> bytes:
    """Replace the signing key so all existing administrator sessions expire."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(_KEY_BYTES)
    _write_session_key(target, key)
    return key
