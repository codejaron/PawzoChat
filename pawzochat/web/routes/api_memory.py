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

"""REST API for per-persona memory CRUD."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from pawzochat.web.routes import get_app

api_memory_bp = Blueprint("api_memory", __name__)

# Match the built-in tool's order of magnitude; manual entry is a bit more
# generous.
_SUMMARY_MAX_CHARS = 2000


def _validate_summary(value) -> tuple[str, str]:
    """Return (summary, error); a non-empty error means validation failed."""
    if not isinstance(value, str):
        return "", "记忆内容必须是字符串"
    summary = value.strip()
    if not summary:
        return "", "记忆内容不能为空"
    if len(summary) > _SUMMARY_MAX_CHARS:
        return "", f"记忆内容过长（最多 {_SUMMARY_MAX_CHARS} 字符）"
    return summary, ""


@api_memory_bp.route("/<persona_id>/memories", methods=["GET"])
def list_memories(persona_id: str):
    app = get_app()
    personas_cfg = app.config.get("personas", default={})
    if persona_id not in personas_cfg:
        return jsonify({"error": "Persona not found"}), 404

    data = app.memory_service.load_memories(persona_id)
    memories = data.get("memories", [])
    memories_sorted = sorted(
        enumerate(memories),
        key=lambda pair: pair[1].get("created_at", ""),
        reverse=True,
    )
    result = []
    for idx, m in memories_sorted:
        result.append({
            "index": idx,
            "summary": m.get("summary", ""),
            "importance": m.get("importance", 3),
            "created_at": m.get("created_at", ""),
        })
    return jsonify({"memories": result, "total": len(memories)})


@api_memory_bp.route("/<persona_id>/memories", methods=["POST"])
def add_memory(persona_id: str):
    app = get_app()
    personas_cfg = app.config.get("personas", default={})
    if persona_id not in personas_cfg:
        return jsonify({"error": "Persona not found"}), 404

    data = request.get_json(force=True)
    summary, err = _validate_summary(data.get("summary", ""))
    if err:
        return jsonify({"error": err}), 400

    try:
        importance = int(data.get("importance", 3))
    except (TypeError, ValueError):
        return jsonify({"error": "importance 必须是整数"}), 400
    created_at = data.get("created_at", "")
    if not isinstance(created_at, str):
        return jsonify({"error": "created_at 必须是字符串"}), 400

    entry, _ = app.memory_service.add_memory(
        persona_id, summary, importance, created_at,
    )
    return jsonify({"ok": True, "memory": entry}), 201


@api_memory_bp.route("/<persona_id>/memories/<int:index>", methods=["PUT"])
def update_memory(persona_id: str, index: int):
    app = get_app()
    personas_cfg = app.config.get("personas", default={})
    if persona_id not in personas_cfg:
        return jsonify({"error": "Persona not found"}), 404

    data = request.get_json(force=True)
    updates = {}
    if "summary" in data:
        s, err = _validate_summary(data["summary"])
        if err:
            return jsonify({"error": err}), 400
        updates["summary"] = s
    if "importance" in data:
        try:
            updates["importance"] = int(data["importance"])
        except (TypeError, ValueError):
            return jsonify({"error": "importance 必须是整数"}), 400
    if "created_at" in data:
        if not isinstance(data["created_at"], str):
            return jsonify({"error": "created_at 必须是字符串"}), 400
        updates["created_at"] = data["created_at"]

    if not updates:
        return jsonify({"error": "没有需要更新的字段"}), 400

    ok = app.memory_service.update_memory(persona_id, index, updates)
    if not ok:
        return jsonify({"error": "记忆条目不存在"}), 404
    return jsonify({"ok": True})


@api_memory_bp.route("/<persona_id>/memories/<int:index>", methods=["DELETE"])
def delete_memory(persona_id: str, index: int):
    app = get_app()
    personas_cfg = app.config.get("personas", default={})
    if persona_id not in personas_cfg:
        return jsonify({"error": "Persona not found"}), 404

    ok = app.memory_service.delete_memory(persona_id, index)
    if not ok:
        return jsonify({"error": "记忆条目不存在"}), 404
    return jsonify({"ok": True})
