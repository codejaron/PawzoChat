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

"""REST API for global settings — read / partial update."""

from __future__ import annotations

import copy
import random
import secrets
import socket

from flask import Blueprint, jsonify, request

from pawzochat.paths import THEME_DIR
from pawzochat.runtime import normalize_https_origin
from pawzochat.utils.crypto import hash_password, validate_password
from pawzochat.web.access import is_legacy_public_access
from pawzochat.web.routes import get_app

api_settings_bp = Blueprint("api_settings", __name__)

EXPOSED_SECTIONS = [
    "chat", "reply", "web", "theme", "notifications",
]


def _clean_active_themes(names) -> list[str]:
    """Drop any names that don't correspond to an existing theme directory."""
    if not isinstance(names, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in names:
        if not isinstance(x, str) or not x or x in seen:
            continue
        if ".." in x or "/" in x or "\\" in x:
            continue
        if (THEME_DIR / x / "style.css").is_file():
            out.append(x)
            seen.add(x)
    return out

_WEB_PATCH_FIELDS = {
    "password",
    "public_enabled",
    "reverse_proxy_enabled",
    "public_base_url",
}
def _generate_port() -> int:
    """Pick a random port in 10000-60000 that is currently available."""
    for _ in range(50):
        port = random.randint(10000, 60000)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return random.randint(10000, 60000)


def _generate_secret() -> str:
    return secrets.token_urlsafe(12)


@api_settings_bp.route("", methods=["GET"])
def get_settings():
    app = get_app()
    is_public = is_legacy_public_access()
    result = {}
    for key in EXPOSED_SECTIONS:
        if key == "web" and is_public:
            continue
        result[key] = copy.deepcopy(app.config.get(key, default=None))
    if "web" in result and result["web"]:
        result["web"]["has_password"] = bool(result["web"].get("password"))
        result["web"].pop("password", None)
    result["is_public"] = is_public
    result["deployment_mode"] = app.runtime.mode.value
    if app.runtime.is_server:
        result["server"] = {
            "bind_host": app.runtime.bind_host,
            "port": app.runtime.port,
            "public_url": app.runtime.public_url,
            "proxy_hops": app.runtime.proxy_hops,
        }
    return jsonify(result)


@api_settings_bp.route("", methods=["PATCH"])
def update_settings():
    app = get_app()
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"error": "settings patch must be an object"}), 400
    is_public = is_legacy_public_access()

    with app.config.lock:
        if "web" in data:
            if app.runtime.is_server:
                return jsonify({
                    "error": "服务器部署参数只能通过 /etc/pawzochat/server.env 和 server passwd 修改",
                }), 403
            if is_public:
                return jsonify({"error": "网络设置仅限本地访问修改"}), 403

            web_patch = data["web"]
            if not isinstance(web_patch, dict):
                return jsonify({"error": "web settings must be an object"}), 400
            unknown = set(web_patch) - _WEB_PATCH_FIELDS
            if unknown:
                return jsonify({"error": "unknown web setting"}), 400

            # Validate a private copy so a rejected multi-field request cannot
            # leave partially mutated settings in memory.
            web_cfg = copy.deepcopy(app.config.data.setdefault("web", {}))

            if "password" in web_patch:
                pw = web_patch["password"]
                if not isinstance(pw, str):
                    return jsonify({"error": "password must be a string"}), 400
                if pw:
                    err = validate_password(pw)
                    if err:
                        return jsonify({"error": err}), 400
                    web_cfg["password"] = hash_password(pw)
                else:
                    web_cfg["password"] = ""
                    web_cfg["public_enabled"] = False

            if "public_base_url" in web_patch:
                public_base_url = web_patch["public_base_url"]
                if not isinstance(public_base_url, str):
                    return jsonify({
                        "error": "public_base_url must be a string",
                    }), 400
                normalized = (
                    "" if not public_base_url.strip()
                    else normalize_https_origin(public_base_url)
                )
                if normalized is None:
                    return jsonify({
                        "error": "公网 HTTPS 地址格式应为 https://chat.example.com，不能包含路径、参数或账号信息",
                    }), 400
                web_cfg["public_base_url"] = normalized

            if "reverse_proxy_enabled" in web_patch:
                reverse_proxy = web_patch["reverse_proxy_enabled"]
                if not isinstance(reverse_proxy, bool):
                    return jsonify({
                        "error": "reverse_proxy_enabled must be a boolean",
                    }), 400
                web_cfg["reverse_proxy_enabled"] = reverse_proxy

            if "public_enabled" in web_patch:
                want_public = web_patch["public_enabled"]
                if not isinstance(want_public, bool):
                    return jsonify({"error": "public_enabled must be a boolean"}), 400
                if want_public and not web_cfg.get("password"):
                    return jsonify({"error": "请先设置访问密码"}), 400
                web_cfg["public_enabled"] = want_public
                if want_public:
                    if not web_cfg.get("public_port"):
                        web_cfg["public_port"] = _generate_port()
                    if not web_cfg.get("public_secret"):
                        web_cfg["public_secret"] = _generate_secret()

            if (
                web_cfg.get("public_enabled")
                and web_cfg.get("reverse_proxy_enabled")
                and not web_cfg.get("public_base_url")
            ):
                return jsonify({
                    "error": "请先填写并保存公网 HTTPS 地址",
                }), 400

            app.config.data["web"] = web_cfg

            data = {k: v for k, v in data.items() if k != "web"}

        if "notifications" in data:
            notification_patch = data["notifications"]
            if not isinstance(notification_patch, dict):
                return jsonify({
                    "error": "notification settings must be an object",
                }), 400
            unknown = set(notification_patch) - {"hide_content"}
            if unknown:
                return jsonify({"error": "unknown notification setting"}), 400
            if "hide_content" in notification_patch and not isinstance(
                notification_patch["hide_content"], bool
            ):
                return jsonify({"error": "hide_content must be a boolean"}), 400

        for key, value in data.items():
            if key not in EXPOSED_SECTIONS:
                continue
            if key == "theme" and isinstance(value, dict):
                theme_cfg = app.config.data.setdefault("theme", {})
                if "mode" in value and value["mode"] in ("light", "dark", "auto"):
                    theme_cfg["mode"] = value["mode"]
                if "active" in value:
                    theme_cfg["active"] = _clean_active_themes(value["active"])
                continue
            if isinstance(value, dict) and isinstance(app.config.data.get(key), dict):
                app.config.data[key].update(value)
            else:
                app.config.data[key] = value

        app.config.save()

    result = {"ok": True}
    if not is_public:
        web_out = copy.deepcopy(app.config.get("web", default={}))
        web_out["has_password"] = bool(web_out.get("password"))
        web_out.pop("password", None)
        result["web"] = web_out
    result["notifications"] = copy.deepcopy(
        app.config.get("notifications", default={})
    )
    return jsonify(result)


@api_settings_bp.route("/regenerate-public", methods=["POST"])
def regenerate_public():
    app = get_app()
    if app.runtime.is_server:
        return jsonify({"error": "服务器模式不使用随机公网端口和路径"}), 403
    if is_legacy_public_access():
        return jsonify({"error": "网络设置仅限本地访问修改"}), 403
    with app.config.lock:
        web_cfg = app.config.data.setdefault("web", {})
        old_secret = web_cfg.get("public_secret", "")
        web_cfg["public_port"] = _generate_port()
        web_cfg["public_secret"] = _generate_secret()
        app.config.save()

    if old_secret and app.web_push_service:
        app.web_push_service.remove_scope_path(f"/{old_secret}")

    web_out = copy.deepcopy(web_cfg)
    web_out["has_password"] = bool(web_out.get("password"))
    web_out.pop("password", None)
    return jsonify({"ok": True, "web": web_out})
