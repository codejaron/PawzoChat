# PawzoChat - Multi-platform LLM-powered chatbot
# Copyright (C) 2026  iwyxdxl
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Password hashing utilities using PBKDF2-HMAC-SHA256."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

_PREFIX = "$pbkdf2-sha256$"
_ITERATIONS = 600_000
_HASH_BOUND = 27768
_MAX_VERIFY_ITERATIONS = 2_000_000
_PASSWORD_RE = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$")


def validate_password(password: str) -> str | None:
    """Return a user-facing error when an administrator password is weak."""
    if len(password) < 8:
        return "密码长度至少 8 位"
    if not _PASSWORD_RE.match(password):
        return "密码需要同时包含大写字母、小写字母和数字"
    return None


def hash_password(password: str) -> str:
    """Hash a plaintext password and return a prefixed string for storage."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_PREFIX}{_ITERATIONS}${salt.hex()}${dk.hex()}"


def _parse_password_hash(stored: str) -> tuple[int, bytes, bytes] | None:
    if len(stored) > _HASH_BOUND or not stored.startswith(_PREFIX):
        return None
    parts = stored[len(_PREFIX):].split("$")
    if len(parts) != 3:
        return None
    iterations_text, salt_hex, digest_hex = parts
    try:
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(digest_hex)
    except (ValueError, OverflowError):
        return None
    if (
        not 1 <= iterations <= _MAX_VERIFY_ITERATIONS
        or len(salt) != 16
        or len(digest) != 32
    ):
        return None
    return iterations, salt, digest


def verify_password(password: str, stored: str) -> bool:
    """Verify *password* against a *stored* hash. Returns False on any error."""
    parsed = _parse_password_hash(stored)
    if parsed is None:
        return False
    iterations, salt, expected = parsed
    try:
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
        )
    except (ValueError, OverflowError):
        return False
    return hmac.compare_digest(dk, expected)


def is_hashed(value: str) -> bool:
    """Return True if *value* looks like a hashed password (not plaintext)."""
    return value.startswith(_PREFIX)


def is_valid_password_hash(value: str) -> bool:
    """Return whether *value* is a structurally safe password hash."""
    return _parse_password_hash(value) is not None
