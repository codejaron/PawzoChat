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

"""Message sending with typing simulation and segmented delivery."""

from __future__ import annotations

import logging
import random
import time

from pawzochat.transport.client import ILinkClient
from pawzochat.transport.models import TypingStatus

logger = logging.getLogger(__name__)

SEND_IMAGE_MAX_RETRIES = 3
SEND_FILE_MAX_RETRIES = 3


class MessageSender:
    """High-level message sending with optional typing simulation."""

    def __init__(self, client: ILinkClient, reply_config: dict | None = None):
        self.client = client
        cfg = reply_config or {}
        self.typing_delay_enabled: bool = cfg.get("typing_delay_enabled", True)
        self.typing_speed: float = cfg.get("typing_speed", 0.2)
        self.typing_speed_random_min: float = cfg.get("typing_speed_random_min", 0.05)
        self.typing_speed_random_max: float = cfg.get("typing_speed_random_max", 0.1)
        self.split_by_newline: bool = cfg.get("split_by_newline", True)
        self.show_typing: bool = cfg.get("show_typing_indicator", True)

        self._typing_tickets: dict[str, str] = {}

    def send_text(self, to_user_id: str, text: str, context_token: str) -> bool:
        """Send a simple text message (no typing simulation)."""
        msg = ILinkClient.build_text_message(to_user_id, text, context_token)
        try:
            self.client.send_message(msg)
            return True
        except Exception:
            logger.exception("发送文本消息失败: to=%s", to_user_id)
            return False

    def send_image(self, to_user_id: str, image_path: str, context_token: str) -> bool:
        """Upload an image to CDN and send it as a WeChat image message."""
        from pawzochat.transport.cdn import upload_image

        for attempt in range(1, SEND_IMAGE_MAX_RETRIES + 1):
            try:
                cdn_info = upload_image(self.client, image_path, to_user_id)
                msg = ILinkClient.build_image_message(to_user_id, cdn_info, context_token)
                self.client.send_message(msg)
                logger.info("图片发送成功: to=%s file=%s", to_user_id, image_path)
                return True
            except Exception:
                if attempt < SEND_IMAGE_MAX_RETRIES:
                    logger.warning(
                        "图片发送失败 (attempt %d/%d): to=%s",
                        attempt, SEND_IMAGE_MAX_RETRIES, to_user_id,
                        exc_info=True,
                    )
                else:
                    logger.exception(
                        "图片发送失败 (已重试 %d 次): to=%s",
                        SEND_IMAGE_MAX_RETRIES, to_user_id,
                    )
        return False

    def send_file(
        self,
        to_user_id: str,
        file_path: str,
        context_token: str,
        file_name: str = "",
    ) -> bool:
        """Upload a non-image file to CDN and send it as a WeChat file message."""
        from pawzochat.transport.cdn import upload_file

        for attempt in range(1, SEND_FILE_MAX_RETRIES + 1):
            try:
                cdn_info = upload_file(
                    self.client,
                    file_path,
                    to_user_id,
                    file_name=file_name,
                )
                msg = ILinkClient.build_file_message(
                    to_user_id,
                    cdn_info,
                    context_token,
                    file_name=file_name,
                )
                self.client.send_message(msg)
                logger.info(
                    "文件发送成功: to=%s file=%s",
                    to_user_id,
                    file_name or file_path,
                )
                return True
            except Exception:
                if attempt < SEND_FILE_MAX_RETRIES:
                    logger.warning(
                        "文件发送失败 (attempt %d/%d): to=%s",
                        attempt, SEND_FILE_MAX_RETRIES, to_user_id,
                        exc_info=True,
                    )
                else:
                    logger.exception(
                        "文件发送失败 (已重试 %d 次): to=%s",
                        SEND_FILE_MAX_RETRIES, to_user_id,
                    )
        return False

    def send_reply(
        self,
        to_user_id: str,
        text: str,
        context_token: str,
        ilink_user_id: str = "",
        *,
        split_text: bool = True,
    ):
        """Send a reply with typing simulation and optional segmented delivery.

        1. Split text by newlines (if configured)
        2. For each segment: show typing → delay by char count → send text → cancel typing
        """
        if not text.strip():
            return

        segments = self._split_segments(text) if split_text else [text]

        for i, segment in enumerate(segments):
            segment = segment.strip()
            if not segment:
                continue

            self.send_one_reply(
                to_user_id,
                segment,
                context_token,
                ilink_user_id,
            )

            if i < len(segments) - 1:
                time.sleep(random.uniform(0.3, 0.8))

    def send_one_reply(
        self,
        to_user_id: str,
        text: str,
        context_token: str,
        ilink_user_id: str = "",
        *,
        is_first: bool = False,
    ) -> bool:
        """Send one prepared text reply with typing simulation and no re-splitting."""
        if not text.strip():
            return False

        if self.show_typing and ilink_user_id:
            self._send_typing(ilink_user_id, context_token, TypingStatus.TYPING)

        if self.typing_delay_enabled and not is_first:
            time.sleep(self._calculate_delay(text))

        ok = self.send_text(to_user_id, text, context_token)

        if self.show_typing and ilink_user_id:
            self._send_typing(ilink_user_id, context_token, TypingStatus.CANCEL)

        return ok

    def _split_segments(self, text: str) -> list[str]:
        if not self.split_by_newline:
            return [text]
        segments = [s for s in text.split("\n") if s.strip()]
        if not segments:
            return [text]
        return segments

    def _calculate_delay(self, text: str) -> float:
        return self.estimate_delay_from_config(
            text,
            {
                "typing_speed": self.typing_speed,
                "typing_speed_random_min": self.typing_speed_random_min,
                "typing_speed_random_max": self.typing_speed_random_max,
            },
        )

    @staticmethod
    def estimate_delay_from_config(text: str, reply_config: dict | None = None) -> float:
        cfg = reply_config or {}
        typing_speed = float(cfg.get("typing_speed", 0.2))
        speed_min = float(cfg.get("typing_speed_random_min", 0.05))
        speed_max = float(cfg.get("typing_speed_random_max", 0.1))
        base = len(text) * typing_speed
        jitter = random.uniform(speed_min, speed_max)
        delay = base + jitter * len(text)
        return min(max(delay, 0.5), 8.0)

    def _send_typing(self, ilink_user_id: str, context_token: str, status: int):
        try:
            ticket = self._get_typing_ticket(ilink_user_id, context_token)
            if ticket:
                self.client.send_typing(ilink_user_id, ticket, status)
        except Exception:
            logger.debug("发送 typing 状态失败", exc_info=True)

    def _get_typing_ticket(self, ilink_user_id: str, context_token: str) -> str:
        cached = self._typing_tickets.get(ilink_user_id)
        if cached:
            return cached
        try:
            resp = self.client.get_config(ilink_user_id, context_token)
            ticket = resp.get("typing_ticket", "")
            if ticket:
                self._typing_tickets[ilink_user_id] = ticket
            return ticket
        except Exception:
            logger.debug("获取 typing_ticket 失败", exc_info=True)
            return ""
