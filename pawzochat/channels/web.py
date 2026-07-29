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

"""Web preview channel — local delivery with typing-delay simulation.

The web UI receives assistant messages over SSE (broadcast by the reply
dispatcher), so this channel does no network I/O. It only reproduces the
human-like pacing between message bubbles that used to live inline in
``ReplyDispatcher._delay_for_local_preview``.
"""

from __future__ import annotations

import time

from pawzochat.channels.base import Channel
from pawzochat.transport.sender import MessageSender


class WebChannel(Channel):
    channel_type = "web"
    display_name = "网页"

    def deliver_message(
        self,
        persona_id: str,
        message: dict,
        reply_ctx: dict | None = None,
        *,
        is_first: bool = False,
        is_last: bool = False,
    ) -> bool:
        if not is_first:
            self._delay_for_local_preview(message)
        return True

    def _delay_for_local_preview(self, message: dict) -> None:
        reply_cfg = self._app.config.get("reply", default={})
        if not reply_cfg.get("typing_delay_enabled", True):
            return

        content = message.get("content", [])
        if any(block.get("type") in {"emoji", "image", "file", "voice"} for block in content):
            time.sleep(0.6)
            return
        text = "".join(
            block.get("text", "")
            for block in content
            if block.get("type") == "text"
        )
        if text.strip():
            time.sleep(MessageSender.estimate_delay_from_config(text, reply_cfg))
