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

"""HTTP client for QQ Bot API v2 — access token, send, rich-media upload.

One instance per QQ account. Thread-safe access-token caching (the gateway
thread and the reply-delivery thread share a client).
"""

from __future__ import annotations

import base64
import logging
import threading
import time

import requests

from pawzochat.transport.qq.models import (
    FILE_TYPE_FILE,
    FILE_TYPE_IMAGE,
    MSG_TYPE_TEXT,
)

logger = logging.getLogger(__name__)

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
PROD_BASE_URL = "https://api.sgroup.qq.com"
SANDBOX_BASE_URL = "https://sandbox.api.sgroup.qq.com"

# Refresh this many seconds before the token actually expires (the platform
# grants a fresh token within the last 60s while keeping the old one valid).
_REFRESH_SKEW_SECONDS = 60


class QQClientError(RuntimeError):
    """Raised when a QQ API call fails (bad creds, rate limit, etc.)."""


class QQClient:
    def __init__(self, app_id: str, app_secret: str, *, sandbox: bool = False):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = SANDBOX_BASE_URL if sandbox else PROD_BASE_URL
        self._session = requests.Session()
        self._token_lock = threading.Lock()
        self._access_token = ""
        self._expires_at = 0.0

    # ---- Access token ----

    def get_access_token(self, *, force: bool = False) -> str:
        """Return a valid access token, refreshing if near expiry.

        Raises :class:`QQClientError` if the credentials are rejected.
        """
        with self._token_lock:
            now = time.time()
            if (
                not force
                and self._access_token
                and now < self._expires_at - _REFRESH_SKEW_SECONDS
            ):
                return self._access_token
            try:
                resp = self._session.post(
                    TOKEN_URL,
                    json={
                        "appId": self.app_id,
                        "clientSecret": self.app_secret,
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                raise QQClientError(f"获取 QQ access_token 失败: {exc}") from exc

            token = data.get("access_token", "")
            if not token:
                raise QQClientError(
                    f"QQ access_token 响应异常: {data}"
                )
            try:
                expires_in = int(data.get("expires_in", 7200))
            except (TypeError, ValueError):
                expires_in = 7200
            self._access_token = token
            self._expires_at = time.time() + expires_in
            return token

    def _auth_headers(self) -> dict:
        return {"Authorization": f"QQBot {self.get_access_token()}"}

    def _invalidate_token(self) -> None:
        with self._token_lock:
            self._access_token = ""
            self._expires_at = 0.0

    def invalidate_access_token(self) -> None:
        """Discard the cached token after a gateway authentication failure."""
        self._invalidate_token()

    def _auth_get(self, url: str, *, timeout) -> requests.Response:
        """GET with one forced token refresh on a 401/403."""
        resp = self._session.get(
            url, headers=self._auth_headers(), timeout=timeout,
        )
        if resp.status_code in (401, 403):
            self._invalidate_token()
            resp = self._session.get(
                url, headers=self._auth_headers(), timeout=timeout,
            )
        return resp

    def _auth_post(self, url: str, payload: dict, *, timeout) -> requests.Response:
        """POST with the bearer token, force-refreshing once on a 401/403 (the
        token may have been revoked before its natural expiry) before failing."""
        resp = self._session.post(
            url, json=payload, headers=self._auth_headers(), timeout=timeout,
        )
        if resp.status_code in (401, 403):
            self._invalidate_token()
            resp = self._session.post(
                url, json=payload, headers=self._auth_headers(), timeout=timeout,
            )
        return resp

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        try:
            self._session.close()
        except Exception:
            pass

    # ---- Gateway ----

    def get_gateway(self) -> str:
        """Return the WebSocket gateway URL (wss://...)."""
        # The current QQ Bot API uses /gateway. Keep the legacy route as a
        # narrow compatibility fallback for sandbox/older deployments.
        url = f"{self.base_url}/gateway"
        resp = self._auth_get(url, timeout=10)
        if resp.status_code in (404, 405):
            url = f"{self.base_url}/gateway/bot"
            resp = self._auth_get(url, timeout=10)
        resp.raise_for_status()
        gateway = resp.json().get("url", "")
        if not gateway:
            raise QQClientError(f"QQ {url.removeprefix(self.base_url)} 未返回 url")
        return gateway

    # ---- Sending ----

    def send_c2c_message(
        self,
        openid: str,
        *,
        content: str = "",
        msg_type: int = MSG_TYPE_TEXT,
        msg_id: str = "",
        msg_seq: int = 1,
        media: dict | None = None,
    ) -> dict:
        """Send a C2C (private) message. ``msg_id`` ties it to an inbound
        message for a passive reply; ``msg_seq`` is a 16-bit deduplication key.
        """
        url = f"{self.base_url}/v2/users/{openid}/messages"
        payload: dict = {"msg_type": msg_type, "content": content}
        if msg_id:
            payload["msg_id"] = msg_id
            payload["msg_seq"] = msg_seq
        if media is not None:
            payload["media"] = media
        resp = self._auth_post(url, payload, timeout=15)
        if resp.status_code >= 400:
            raise QQClientError(
                f"QQ 发送消息失败 status={resp.status_code} body={resp.text[:300]}"
            )
        return resp.json() if resp.content else {}

    def upload_c2c_media(
        self,
        openid: str,
        file_data: bytes,
        *,
        file_type: int = FILE_TYPE_IMAGE,
        file_name: str = "",
    ) -> dict:
        """Upload rich media for a C2C message (base64). Returns the response
        whose ``file_info`` is referenced in a later ``msg_type=7`` send.
        """
        url = f"{self.base_url}/v2/users/{openid}/files"
        payload = {
            "file_type": file_type,
            "srv_send_msg": False,
            "file_data": base64.b64encode(file_data).decode("ascii"),
        }
        if file_type == FILE_TYPE_FILE and file_name:
            payload["file_name"] = _sanitize_file_name(file_name)
        resp = self._auth_post(url, payload, timeout=30)
        if resp.status_code >= 400:
            raise QQClientError(
                f"QQ 上传媒体失败 status={resp.status_code} body={resp.text[:300]}"
            )
        return resp.json() if resp.content else {}


def _sanitize_file_name(file_name: str) -> str:
    """Return a QQ-safe basename while preserving readable Unicode names."""
    name = str(file_name).replace("\\", "/").rsplit("/", 1)[-1]
    invalid = '<>:"/\\|?*'
    name = "".join(
        char if ord(char) >= 32 and char not in invalid else "_"
        for char in name
    ).strip(" .")
    return (name or "file")[:255]
