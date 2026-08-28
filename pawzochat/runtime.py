# PawzoChat - Multi-platform LLM-powered chatbot
# Copyright (C) 2026  iwyxdxl
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Process-level runtime options for desktop and headless server modes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit


class RuntimeConfigurationError(ValueError):
    """Raised when deployment-owned runtime settings are invalid."""


class RuntimeMode(str, Enum):
    DESKTOP = "desktop"
    SERVER = "server"


def normalize_https_origin(value: str) -> str | None:
    """Return a canonical HTTPS origin, or ``None`` when invalid.

    Deployment URLs are origins rather than application configuration: paths,
    credentials, query strings, and fragments are deliberately rejected.
    """
    value = str(value or "").strip()
    if not value or len(value) > 2048 or any(ch.isspace() for ch in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        ascii_host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if not ascii_host or len(ascii_host) > 253 or "\\" in ascii_host:
        return None
    host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    netloc = f"{host}:{port}" if port is not None else host
    return f"https://{netloc}"


def _normalize_bind_host(value: str) -> str:
    value = str(value or "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise RuntimeConfigurationError(
            "PAWZOCHAT_BIND must be an IPv4 or IPv6 address"
        ) from exc


def _normalize_port(value: int | str) -> int:
    if isinstance(value, bool):
        raise RuntimeConfigurationError("PAWZOCHAT_PORT must be an integer")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigurationError(
            "PAWZOCHAT_PORT must be an integer"
        ) from exc
    if not 1 <= port <= 65535:
        raise RuntimeConfigurationError(
            "PAWZOCHAT_PORT must be between 1 and 65535"
        )
    return port


def _normalize_proxy_hops(value: int | str) -> int:
    if isinstance(value, bool):
        raise RuntimeConfigurationError("PAWZOCHAT_PROXY_HOPS must be an integer")
    try:
        hops = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigurationError(
            "PAWZOCHAT_PROXY_HOPS must be an integer"
        ) from exc
    if not 0 <= hops <= 2:
        raise RuntimeConfigurationError(
            "PAWZOCHAT_PROXY_HOPS must be between 0 and 2"
        )
    return hops


def normalize_data_dir(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if path == Path(path.anchor):
        raise RuntimeConfigurationError(
            "PAWZOCHAT_DATA_DIR cannot be a filesystem root"
        )
    return path


@dataclass(frozen=True)
class RuntimeOptions:
    """Immutable deployment options passed into the application lifecycle."""

    mode: RuntimeMode
    data_dir: Path | None = None
    bind_host: str = "127.0.0.1"
    port: int = 62000
    public_url: str = ""
    proxy_hops: int = 0

    @property
    def is_server(self) -> bool:
        return self.mode is RuntimeMode.SERVER

    @classmethod
    def desktop(cls) -> "RuntimeOptions":
        return cls(mode=RuntimeMode.DESKTOP)

    @classmethod
    def server(
        cls,
        *,
        data_dir: str | Path,
        bind_host: str = "127.0.0.1",
        port: int | str = 62000,
        public_url: str,
        proxy_hops: int | str = 1,
    ) -> "RuntimeOptions":
        normalized_url = normalize_https_origin(public_url)
        if normalized_url is None:
            raise RuntimeConfigurationError(
                "PAWZOCHAT_PUBLIC_URL must be an HTTPS origin such as "
                "https://chat.example.com"
            )
        return cls(
            mode=RuntimeMode.SERVER,
            data_dir=normalize_data_dir(data_dir),
            bind_host=_normalize_bind_host(bind_host),
            port=_normalize_port(port),
            public_url=normalized_url,
            proxy_hops=_normalize_proxy_hops(proxy_hops),
        )
