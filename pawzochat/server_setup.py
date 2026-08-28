# PawzoChat - Multi-platform LLM-powered chatbot
# Copyright (C) 2026  iwyxdxl
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Idempotent, non-GUI initialization of a PawzoChat server data directory."""

from __future__ import annotations

from pathlib import Path
import shutil

from pawzochat.core.config import ConfigManager
from pawzochat.paths import (
    AUTH_DIR,
    BUNDLED_DATA_DIR,
    CONFIG_PATH,
    DATA_DIR,
    SESSION_KEY_PATH,
    VAPID_PRIVATE_KEY_PATH,
)
from pawzochat.services.web_push import ensure_vapid_identity
from pawzochat.utils.crypto import (
    hash_password,
    is_hashed,
    is_valid_password_hash,
    validate_password,
)
from pawzochat.web.session_key import load_or_create_session_key, rotate_session_key


_MUTABLE_DIRECTORIES = (
    "auth",
    "books",
    "chats",
    "config",
    "invitation",
    "logs",
    "moments",
    "plugins",
    "profile",
    "prompts",
    "push",
)
_SEEDED_DIRECTORIES = ("emoji", "mcp_servers", "theme")


def _copy_missing_tree(source: Path, destination: Path) -> None:
    """Copy bundled files without overwriting user-modified resources."""
    if not source.is_dir():
        return
    for source_path in source.rglob("*"):
        relative = source_path.relative_to(source)
        destination_path = destination / relative
        if source_path.is_dir():
            destination_path.mkdir(parents=True, exist_ok=True)
        elif source_path.is_file() and not destination_path.exists():
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)


def ensure_server_layout() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.chmod(0o700)

    for name in _MUTABLE_DIRECTORIES:
        path = DATA_DIR / name
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)

    if BUNDLED_DATA_DIR != DATA_DIR:
        for name in _SEEDED_DIRECTORIES:
            _copy_missing_tree(BUNDLED_DATA_DIR / name, DATA_DIR / name)


def load_server_config() -> ConfigManager:
    config = ConfigManager()
    config.load()
    return config


def has_admin_password() -> bool:
    if not CONFIG_PATH.is_file():
        return False
    config = load_server_config()
    password = str(config.get("web", "password", default="") or "")
    return bool(password and is_valid_password_hash(password))


def set_admin_password(password: str) -> None:
    error = validate_password(password)
    if error:
        raise ValueError(error)
    config = load_server_config()
    web = config.data.setdefault("web", {})
    web["password"] = hash_password(password)
    config.save()
    rotate_session_key(SESSION_KEY_PATH)
    CONFIG_PATH.chmod(0o600)


def initialize_server(password: str | None = None) -> dict:
    """Create all runtime state without starting network listeners."""
    ensure_server_layout()
    config = load_server_config()
    web = config.data.setdefault("web", {})
    existing_password = str(web.get("password", "") or "")

    if password is not None:
        error = validate_password(password)
        if error:
            raise ValueError(error)
        web["password"] = hash_password(password)
    elif existing_password and not is_hashed(existing_password):
        raise ValueError("检测到旧版明文密码；服务器初始化时必须重新设置管理员密码")
    elif existing_password and not is_valid_password_hash(existing_password):
        raise ValueError("管理员密码哈希已损坏；服务器初始化时必须重新设置密码")
    elif not existing_password:
        raise ValueError("服务器模式尚未设置管理员密码")

    # Legacy desktop-public settings are deployment-inapplicable in server
    # mode. Clearing the enable flag prevents an accidental second listener if
    # this data directory is later opened with a desktop command.
    web["public_enabled"] = False
    config.save()
    CONFIG_PATH.chmod(0o600)

    if password is not None and SESSION_KEY_PATH.exists():
        rotate_session_key(SESSION_KEY_PATH)
    else:
        load_or_create_session_key(SESSION_KEY_PATH)
    ensure_vapid_identity(VAPID_PRIVATE_KEY_PATH)
    AUTH_DIR.chmod(0o700)
    SESSION_KEY_PATH.chmod(0o600)
    VAPID_PRIVATE_KEY_PATH.chmod(0o600)

    return {
        "data_dir": str(DATA_DIR),
        "config_path": str(CONFIG_PATH),
        "password_configured": True,
        "session_key_path": str(SESSION_KEY_PATH),
        "vapid_key_path": str(VAPID_PRIVATE_KEY_PATH),
    }


def assert_server_initialized() -> None:
    if not DATA_DIR.is_dir() or not CONFIG_PATH.is_file():
        raise RuntimeError(
            "server data is not initialized; run 'pawzochat server init' first"
        )
    config = load_server_config()
    password = str(config.get("web", "password", default="") or "")
    if not password or not is_valid_password_hash(password):
        raise RuntimeError(
            "server administrator password is missing or invalid; run "
            "'pawzochat server passwd'"
        )
    if not SESSION_KEY_PATH.is_file():
        raise RuntimeError(
            "server session key is missing; run 'pawzochat server init'"
        )
