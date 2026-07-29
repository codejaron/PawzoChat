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

"""Built-in capability tools: ``record_memory`` / ``update_memory``.

Let the chat LLM decide when a conversation moment is worth remembering,
instead of the old fixed-round auto-summarization. Both tools write through
:class:`pawzochat.services.memory.MemoryService`, so the on-disk format
(``data/chats/<persona_id>/memory.json``) is identical to memories created
manually in the web panel.

``update_memory`` targets a memory by its storage index — the same ``#N``
numbers that :meth:`MemoryService.format_memories_for_prompt` annotates in
the ``[历史记忆]`` system block and that the web API exposes.

Handlers access ``app.memory_service`` lazily at call time (it is
constructed after ``register_builtin`` runs in ``App.start()``), mirroring
how the image tool reaches ``app.image_manager``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from pawzochat.llm.base import ContentBlock

if TYPE_CHECKING:
    from pawzochat.app import App

logger = logging.getLogger(__name__)


RECORD_TOOL_NAME = "record_memory"

RECORD_TOOL_DESCRIPTION = (
    "把一件值得长期记住的事情记录为一条新的长期记忆。"
    "适用场景：用户告诉了你关于他/她的重要信息（身份、习惯、喜好、近况、心情），"
    "你们之间达成了新的约定或承诺，发生了对你们关系有意义的事件，"
    "或你产生了不想忘记的重要感受。"
    "记忆内容必须以『我』的第一人称写成，像日记或回忆片段"
    "（如『他告诉我……』『我答应过他/她……』『那天我们聊到……』），"
    "不要写成『用户说了A，我回复了B』式的第三人称摘要。"
    "无关紧要的寒暄和闲聊不要记录；[历史记忆]中已有的内容不要重复记录。"
)

RECORD_TOOL_PARAMETERS: dict = {
    "summary": {
        "type": "string",
        "description": (
            "记忆内容：以『我』的第一人称写成的一段回忆，简短凝练（200字以内），"
            "带有回忆的质感，像日记而不是会议纪要。"
        ),
    },
    "importance": {
        "type": "integer",
        "description": "重要度，1-5 的整数（1=无关紧要的小事，5=对我而言非常重要、不想忘记的事）。",
    },
}


UPDATE_TOOL_NAME = "update_memory"

UPDATE_TOOL_DESCRIPTION = (
    "覆盖改写一条已有的长期记忆。当[历史记忆]中某条记忆已经过时、不再准确，"
    "或同一件事有了新进展需要合并时使用。"
    "index 必须是[历史记忆]中该条记忆标注的『记忆 #N』里的编号 N，原样照抄，不要自己推算。"
    "注意：新的 summary 会完全替换旧内容，所以要把旧记忆里仍然有效的信息一并写进去，"
    "而不是只写新增部分。仍以『我』的第一人称回忆口吻书写。"
)

UPDATE_TOOL_PARAMETERS: dict = {
    "index": {
        "type": "integer",
        "description": "要改写的记忆编号，即[历史记忆]中『记忆 #N』的 N（原样照抄）。",
    },
    "summary": {
        "type": "string",
        "description": "改写后的完整记忆内容（将覆盖原内容），以『我』的第一人称写成，200字以内。",
    },
    "importance": {
        "type": "integer",
        "description": "更新后的重要度（1-5 的整数）；不传则保持原值。",
        # Presence of "default" marks this as optional:
        # get_tool_definitions (adapters.py:225-226) only looks for the key
        # to decide required/optional; the default value is never sent to
        # the LLM.
        "default": None,
    },
}


# Hard cap on memory summary length (chars). The tool description asks for
# ~200 chars; this is a generous safety limit beyond that. Oversized memories
# permanently bloat memory.json and are injected into every subsequent prompt.
SUMMARY_MAX_CHARS = 500


def _err(msg: str) -> list[ContentBlock]:
    return [ContentBlock(type="text", text=msg)]


def _extract_summary(arguments: dict) -> tuple[str, list[ContentBlock] | None]:
    """Validate and extract the ``summary`` argument; returns (summary, error)."""
    raw = arguments.get("summary")
    if raw is not None and not isinstance(raw, str):
        return "", _err("summary 参数必须是字符串。")
    summary = (raw or "").strip()
    if not summary:
        return "", _err("需要非空的 summary。")
    if len(summary) > SUMMARY_MAX_CHARS:
        return "", _err(
            f"summary 过长（{len(summary)} 字符，上限 {SUMMARY_MAX_CHARS}）："
            "请把记忆压缩到200字以内后重试。"
        )
    return summary, None


def _memory_enabled(persona) -> bool:
    mem = getattr(persona, "memory", None)
    return isinstance(mem, dict) and bool(mem.get("enabled"))


def make_handlers(app: App) -> tuple[
    Callable[[dict, dict], list[ContentBlock]],
    Callable[[dict, dict], list[ContentBlock]],
]:
    """Build ``(record_handler, update_handler)`` closures bound to *app*.

    Both callables match ``LocalToolHandler``:
    ``(arguments, context) -> list[ContentBlock]``.
    """

    def _check_context(context: dict) -> tuple[str, list[ContentBlock] | None]:
        """Shared persona/enabled/service guards; returns (persona_id, error)."""
        persona = context.get("persona")
        persona_id = context.get("persona_id") or ""
        if persona is None or not persona_id:
            return "", _err("记忆工具上下文缺失：未提供 persona。")
        # Runtime re-check as a safety measure (mirrors the image generation
        # tool pattern — the persona snapshot may have drifted since tool-list
        # construction).
        if not _memory_enabled(persona):
            return "", _err("当前角色未启用记忆功能。")
        if app.memory_service is None:
            return "", _err("记忆服务尚未就绪。")
        return persona_id, None

    def record_handler(arguments: dict, context: dict) -> list[ContentBlock]:
        persona_id, error = _check_context(context)
        if error:
            return error

        summary, error = _extract_summary(arguments)
        if error:
            return error

        entry, index = app.memory_service.add_memory(
            persona_id, summary, arguments.get("importance", 3),
        )
        # Consolidation is NOT triggered here: it shifts storage indices and
        # would confound a same-round update_memory's #N reference.
        # MessageQueue calls maybe_consolidate after each round instead.
        logger.info(
            "工具记录记忆 persona=%s index=%d importance=%d",
            persona_id, index, entry["importance"],
        )
        app.memory_service.on_memory_recorded(persona_id)
        return [ContentBlock(
            type="text",
            text=(
                f"已记录记忆 #{index}（重要度{entry['importance']}）。"
                "请继续自然地回复用户，不要在回复中提及记忆操作。"
            ),
        )]

    def update_handler(arguments: dict, context: dict) -> list[ContentBlock]:
        persona_id, error = _check_context(context)
        if error:
            return error

        raw_index = arguments.get("index")
        if isinstance(raw_index, bool) or (
            isinstance(raw_index, float) and not raw_index.is_integer()
        ):
            return _err("index 参数无效：必须是[历史记忆]中『记忆 #N』标注的整数编号。")
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return _err("index 参数无效：必须是[历史记忆]中『记忆 #N』标注的整数编号。")
        summary, error = _extract_summary(arguments)
        if error:
            return error

        updates: dict = {"summary": summary}
        if arguments.get("importance") is not None:
            updates["importance"] = arguments["importance"]

        ok = app.memory_service.update_memory(persona_id, index, updates)
        if not ok:
            total = len(
                app.memory_service.load_memories(persona_id).get("memories", [])
            )
            return _err(
                f"记忆 #{index} 不存在（当前共有 {total} 条，编号从 0 开始），"
                "可能刚被整理或删除。不要重试这个编号；"
                "如需保存新内容，请改用 record_memory 记录为新记忆。"
            )
        logger.info("工具更新记忆 persona=%s index=%d", persona_id, index)
        app.memory_service.on_memory_recorded(persona_id)
        return [ContentBlock(
            type="text",
            text=f"已更新记忆 #{index}。请继续自然地回复用户，不要在回复中提及记忆操作。",
        )]

    return record_handler, update_handler
