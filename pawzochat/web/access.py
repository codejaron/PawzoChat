# PawzoChat - Multi-platform LLM-powered chatbot
# Copyright (C) 2026  iwyxdxl
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Request access modes shared by authentication and management routes."""

from __future__ import annotations

from enum import Enum

from flask import request


class AccessMode(str, Enum):
    DESKTOP_LOCAL = "desktop_local"
    DESKTOP_PUBLIC = "desktop_public"
    SERVER_ADMIN = "server_admin"


_ENV_KEY = "pawzochat.access_mode"


class AccessModeMiddleware:
    """Attach a fixed access mode before a request reaches Flask."""

    def __init__(self, wsgi_app, mode: AccessMode):
        self.wsgi_app = wsgi_app
        self.mode = mode.value

    def __call__(self, environ, start_response):
        environ[_ENV_KEY] = self.mode
        return self.wsgi_app(environ, start_response)


def request_access_mode() -> AccessMode:
    raw = request.environ.get(_ENV_KEY, AccessMode.DESKTOP_LOCAL.value)
    try:
        return AccessMode(raw)
    except ValueError:
        return AccessMode.DESKTOP_LOCAL


def is_authenticated_access() -> bool:
    return request_access_mode() in {
        AccessMode.DESKTOP_PUBLIC,
        AccessMode.SERVER_ADMIN,
    }


def is_legacy_public_access() -> bool:
    """Return whether host-level management must remain unavailable."""
    return request_access_mode() is AccessMode.DESKTOP_PUBLIC


def is_server_admin_access() -> bool:
    return request_access_mode() is AccessMode.SERVER_ADMIN


def mark_desktop_public(environ: dict) -> None:
    """Mark a request entering through the legacy secret-prefix endpoint."""
    environ[_ENV_KEY] = AccessMode.DESKTOP_PUBLIC.value
    # Keep the legacy flag during the desktop-public compatibility period.
    environ["pawzochat.is_public"] = True
