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

"""REST API for the current browser's Web Push subscription and presence."""

from __future__ import annotations

import base64
import re
import time
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request

from pawzochat.web.routes import get_app

api_notifications_bp = Blueprint("api_notifications", __name__)

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


def _service():
    service = get_app().web_push_service
    if service is None:
        return None
    return service


def _valid_device_id(value) -> bool:
    return isinstance(value, str) and bool(_DEVICE_ID_RE.fullmatch(value))


def _valid_scope(scope: str) -> bool:
    parsed = urlsplit(scope)
    if not parsed.scheme or not parsed.netloc:
        return False
    expected = urlsplit(request.host_url)
    if parsed.netloc != expected.netloc:
        return False
    if parsed.scheme != expected.scheme:
        return False
    scope_root = (request.script_root or "") + "/"
    return parsed.path.startswith(scope_root)


def _decode_b64url(value: str) -> bytes | None:
    try:
        padding = b"=" * ((4 - len(value) % 4) % 4)
        return base64.urlsafe_b64decode(value.encode("ascii") + padding)
    except (ValueError, UnicodeEncodeError):
        return None


@api_notifications_bp.route("/vapid-public-key", methods=["GET"])
def get_vapid_public_key():
    service = _service()
    if service is None:
        return jsonify({"error": "通知服务未启动"}), 503
    return jsonify({"public_key": service.public_key})


@api_notifications_bp.route("/status", methods=["GET"])
def get_status():
    service = _service()
    if service is None:
        return jsonify({"error": "通知服务未启动"}), 503
    device_id = request.args.get("device_id", "")
    if not _valid_device_id(device_id):
        return jsonify({"error": "invalid device_id"}), 400
    return jsonify(service.subscription_status(device_id))


@api_notifications_bp.route("/subscriptions", methods=["POST"])
def register_subscription():
    service = _service()
    if service is None:
        return jsonify({"error": "通知服务未启动"}), 503
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be an object"}), 400
    device_id = data.get("device_id")
    subscription = data.get("subscription")
    scope = data.get("scope")
    expiration_time = data.get("expiration_time")

    if not _valid_device_id(device_id):
        return jsonify({"error": "invalid device_id"}), 400
    if not isinstance(subscription, dict):
        return jsonify({"error": "invalid subscription"}), 400
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys")
    parsed_endpoint = urlsplit(endpoint) if isinstance(endpoint, str) else None
    if (
        not parsed_endpoint
        or parsed_endpoint.scheme != "https"
        or not parsed_endpoint.netloc
        or len(endpoint) > 4096
        or not isinstance(keys, dict)
    ):
        return jsonify({"error": "invalid subscription endpoint"}), 400
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    decoded_p256dh = _decode_b64url(p256dh) if isinstance(p256dh, str) else None
    decoded_auth = _decode_b64url(auth) if isinstance(auth, str) else None
    if (
        not isinstance(p256dh, str)
        or not isinstance(auth, str)
        or len(p256dh) > 512
        or len(auth) > 128
        or not _B64URL_RE.fullmatch(p256dh)
        or not _B64URL_RE.fullmatch(auth)
        or len(decoded_p256dh or b"") != 65
        or (decoded_p256dh or b"\x00")[0] != 4
        or len(decoded_auth or b"") != 16
    ):
        return jsonify({"error": "invalid subscription keys"}), 400
    if not isinstance(scope, str) or len(scope) > 2048 or not _valid_scope(scope):
        return jsonify({"error": "invalid service worker scope"}), 400
    if expiration_time is not None and (
        not isinstance(expiration_time, (int, float))
        or isinstance(expiration_time, bool)
        or expiration_time <= time.time() * 1000
    ):
        return jsonify({"error": "invalid expiration_time"}), 400

    registered = service.register_subscription(
        device_id,
        {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
        scope,
        expiration_time,
    )
    return jsonify({"ok": True, "subscription": registered}), 201


@api_notifications_bp.route("/unsubscribe", methods=["POST"])
def unregister_subscription():
    service = _service()
    if service is None:
        return jsonify({"error": "通知服务未启动"}), 503
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be an object"}), 400
    device_id = data.get("device_id")
    endpoint = data.get("endpoint", "")
    if not _valid_device_id(device_id):
        return jsonify({"error": "invalid device_id"}), 400
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        return jsonify({"error": "invalid endpoint"}), 400
    removed = service.unregister_subscription(device_id, endpoint)
    return jsonify({"ok": True, "removed": removed})


@api_notifications_bp.route("/presence", methods=["POST"])
def update_presence():
    service = _service()
    if service is None:
        return jsonify({"error": "通知服务未启动"}), 503
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be an object"}), 400
    device_id = data.get("device_id")
    foreground = data.get("foreground")
    if not _valid_device_id(device_id):
        return jsonify({"error": "invalid device_id"}), 400
    if not isinstance(foreground, bool):
        return jsonify({"error": "foreground must be a boolean"}), 400
    known = service.set_presence(device_id, foreground)
    return jsonify({"ok": True, "known_device": known})
