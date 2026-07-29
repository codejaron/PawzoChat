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

"""Long-polling message receiver — one Poller thread per Account."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Callable

from pawzochat.transport.client import ILinkClient
from pawzochat.transport.models import Account, parse_message

if TYPE_CHECKING:
    from pawzochat.transport.auth import AuthManager

logger = logging.getLogger(__name__)

_STALE_TOKEN_ERRCODE = -14
_STALE_TOKEN_PAUSE_SECONDS = 60 * 60


class ILinkAPIError(RuntimeError):
    """Raised when getUpdates returns an API-level failure in an HTTP 200."""


class MessagePoller:
    """Runs a long-poll loop in a daemon thread for one Account."""

    def __init__(
        self,
        account: Account,
        client: ILinkClient,
        auth_manager: AuthManager,
        on_message: Callable[[str, object], None],
        poll_timeout: int = 35,
    ):
        self.account = account
        self.client = client
        self.auth_manager = auth_manager
        self.on_message = on_message
        self.poll_timeout = poll_timeout
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name=f"poller-{self.account.bot_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        logger.info("轮询线程已启动: %s", self.account.bot_id)

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("轮询线程已停止: %s", self.account.bot_id)

    def _poll_loop(self) -> None:
        consecutive_errors = 0
        while self._running:
            try:
                response = self.client.get_updates(
                    buf=self.account.get_updates_buf,
                    timeout=self.poll_timeout,
                )
                self._handle_response(response)
                consecutive_errors = 0
            except Exception:
                if not self._running:
                    break
                consecutive_errors += 1
                wait = min(2 ** consecutive_errors, 60)
                logger.exception(
                    "轮询出错 (连续第%d次)，%d秒后重试",
                    consecutive_errors,
                    wait,
                )
                if self._stop_event.wait(wait):
                    break

    def _handle_response(self, response: dict) -> None:
        errcode = response.get("errcode")
        ret = response.get("ret")
        code = next(
            (value for value in (errcode, ret) if value not in (None, 0, "0")),
            None,
        )
        if code is not None:
            errmsg = response.get("errmsg", "")
            logger.warning(
                "getUpdates API 失败 ret=%s errcode=%s: %s",
                ret,
                errcode,
                errmsg,
            )
            try:
                numeric_code = int(code)
            except (TypeError, ValueError):
                numeric_code = None
            if numeric_code == _STALE_TOKEN_ERRCODE:
                logger.error(
                    "微信 bot_token 暂时失效 (code=-14)，暂停 60 分钟后自动重试"
                )
                self._stop_event.wait(_STALE_TOKEN_PAUSE_SECONDS)
                return
            raise ILinkAPIError(
                f"getUpdates ret={ret} errcode={errcode} errmsg={errmsg}"
            )

        new_buf = response.get("get_updates_buf", "")
        if new_buf:
            self.account.get_updates_buf = new_buf
            self.auth_manager.update_account(self.account)

        timeout_hint = response.get("longpolling_timeout_ms")
        if timeout_hint and isinstance(timeout_hint, int):
            self.poll_timeout = max(timeout_hint // 1000, 10)

        for raw_msg in response.get("msgs", []):
            try:
                message = parse_message(raw_msg)
                if not message.from_user_id:
                    continue

                logger.info(
                    "[%s] 收到消息 from=%s text=%s",
                    self.account.bot_id[:8],
                    message.from_user_id[:12],
                    message.text_content[:50] if message.text_content else "(非文本)",
                )
                self.on_message(self.account.bot_id, message)
            except Exception:
                logger.exception("解析消息失败: %s", raw_msg)
