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

"""Browser Web Push subscriptions, VAPID identity, presence, and delivery."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import queue
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from pawzochat.paths import PUSH_SUBSCRIPTIONS_PATH, VAPID_PRIVATE_KEY_PATH

logger = logging.getLogger(__name__)

# py-vapid's strict validator accepts an HTTPS contact origin but not a URL
# path. The project homepage origin is a real, stable contact point.
_VAPID_SUBJECT = "https://github.com"
_PRESENCE_TTL_SECONDS = 75
_MAX_QUEUE_SIZE = 1000
_STOP = object()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _subscription_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


class WebPushService:
    """Single source of truth for every browser Push subscription.

    Assistant messages enter one FIFO queue. A job may fan out to multiple
    devices in parallel, but the next message does not begin until that fanout
    completes, preserving per-device message order without blocking the reply
    path on external Push services.
    """

    def __init__(
        self,
        config,
        conversation_store,
        *,
        subscriptions_path: str | Path = PUSH_SUBSCRIPTIONS_PATH,
        private_key_path: str | Path = VAPID_PRIVATE_KEY_PATH,
    ):
        self._config = config
        self._conversation_store = conversation_store
        self._subscriptions_path = Path(subscriptions_path)
        self._private_key_path = Path(private_key_path)
        self._lock = threading.RLock()
        self._subscriptions: dict[str, dict] = {}
        self._presence: dict[str, float] = {}
        self._queue: queue.Queue = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._send_pool: ThreadPoolExecutor | None = None

        self._load_subscriptions()
        self._public_key = self._ensure_vapid_key()

    @property
    def public_key(self) -> str:
        return self._public_key

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._send_pool = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="web-push-send",
        )
        self._worker = threading.Thread(
            target=self._run,
            name="web-push-dispatch",
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            logger.error("通知队列已满，停止信号无法入队")
        if self._worker:
            self._worker.join(timeout=2)
        if self._send_pool:
            self._send_pool.shutdown(wait=False, cancel_futures=True)

    # ---- Subscription lifecycle ----

    def register_subscription(
        self,
        device_id: str,
        subscription: dict,
        scope: str,
        expiration_time: float | int | None,
    ) -> dict:
        endpoint = subscription["endpoint"]
        keys = subscription["keys"]
        sid = _subscription_id(endpoint)
        now = _now_iso()
        with self._lock:
            # A browser may rotate its endpoint. Keeping the old endpoint for
            # the same device would produce duplicate notifications until its
            # first 404/410, so replace it at registration time.
            for old_sid, item in list(self._subscriptions.items()):
                if item.get("device_id") == device_id and old_sid != sid:
                    del self._subscriptions[old_sid]

            existing = self._subscriptions.get(sid, {})
            self._subscriptions[sid] = {
                "device_id": device_id,
                "endpoint": endpoint,
                "keys": {
                    "p256dh": keys["p256dh"],
                    "auth": keys["auth"],
                },
                "scope": scope,
                "expiration_time": expiration_time,
                "created_at": existing.get("created_at", now),
                "updated_at": now,
                "last_success_at": existing.get("last_success_at", ""),
                "last_error": "",
                "failure_count": 0,
                "next_retry_at": 0,
            }
            self._persist_locked()
        return {"id": sid, "device_id": device_id, "scope": scope}

    def unregister_subscription(self, device_id: str, endpoint: str = "") -> int:
        removed = 0
        with self._lock:
            for sid, item in list(self._subscriptions.items()):
                if item.get("device_id") != device_id:
                    continue
                if endpoint and item.get("endpoint") != endpoint:
                    continue
                del self._subscriptions[sid]
                removed += 1
            self._presence.pop(device_id, None)
            if removed:
                self._persist_locked()
        return removed

    def remove_scope_path(self, scope_path: str) -> int:
        """Remove subscriptions tied to a regenerated public secret path."""
        wanted = scope_path.rstrip("/") or "/"
        removed = 0
        removed_devices: set[str] = set()
        with self._lock:
            for sid, item in list(self._subscriptions.items()):
                path = urlsplit(item.get("scope", "")).path.rstrip("/") or "/"
                if path == wanted:
                    removed_devices.add(item.get("device_id", ""))
                    del self._subscriptions[sid]
                    removed += 1
            remaining_devices = {
                item.get("device_id", "")
                for item in self._subscriptions.values()
            }
            for device_id in removed_devices - remaining_devices:
                self._presence.pop(device_id, None)
            if removed:
                self._persist_locked()
        if removed:
            logger.info("公网随机路径已更新，清理了 %d 个旧通知订阅", removed)
        return removed

    def subscription_status(self, device_id: str) -> dict:
        with self._lock:
            if self._remove_expired_locked():
                self._persist_locked()
            count = sum(
                1 for item in self._subscriptions.values()
                if item.get("device_id") == device_id
            )
        return {"subscribed": count > 0, "subscription_count": count}

    # ---- Foreground presence ----

    def set_presence(self, device_id: str, foreground: bool) -> bool:
        with self._lock:
            known = any(
                item.get("device_id") == device_id
                for item in self._subscriptions.values()
            )
            if not known:
                self._presence.pop(device_id, None)
                return False
            if foreground:
                self._presence[device_id] = (
                    time.monotonic() + _PRESENCE_TTL_SECONDS
                )
            else:
                self._presence.pop(device_id, None)
            return True

    def _is_foreground_locked(self, device_id: str) -> bool:
        deadline = self._presence.get(device_id, 0)
        if deadline <= time.monotonic():
            self._presence.pop(device_id, None)
            return False
        return True

    # ---- Message dispatch ----

    def enqueue_assistant_message(self, persona_id: str, message: dict) -> bool:
        if message.get("role") != "assistant":
            return False
        with self._lock:
            if not self._subscriptions:
                return False
        job = {
            "persona_id": persona_id,
            "message": copy.deepcopy(message),
        }
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            logger.error(
                "通知队列已满，消息无法进入推送队列 persona=%s timestamp=%s",
                persona_id,
                message.get("timestamp", ""),
            )
            return False
        return True

    def _run(self) -> None:
        while not self._stop_event.is_set():
            job = self._queue.get()
            try:
                if job is _STOP:
                    return
                self._deliver_job(job)
            except Exception:
                logger.exception(
                    "浏览器通知投递任务失败 persona=%s",
                    job.get("persona_id", "") if isinstance(job, dict) else "",
                )
            finally:
                self._queue.task_done()

    def _deliver_job(self, job: dict) -> None:
        persona_id = job["persona_id"]
        if self._conversation_store.notification_muted(persona_id):
            return

        payload = self._build_payload(persona_id, job["message"])
        now_ms = time.time() * 1000
        now_s = time.time()
        changed = False
        targets: list[tuple[str, dict]] = []
        with self._lock:
            changed = self._remove_expired_locked(now_ms=now_ms)
            for sid, item in self._subscriptions.items():
                if self._is_foreground_locked(item.get("device_id", "")):
                    continue
                if float(item.get("next_retry_at", 0) or 0) > now_s:
                    continue
                targets.append((sid, copy.deepcopy(item)))
            if changed:
                self._persist_locked()

        if not targets or not self._send_pool:
            return

        futures = {
            self._send_pool.submit(self._send_one, sid, item, payload): sid
            for sid, item in targets
        }
        results = []
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                # _send_one handles transport failures itself. Reaching here
                # means a programming failure in our own worker and must remain
                # visible rather than being mistaken for an invalid device.
                logger.exception(
                    "通知发送线程异常 subscription=%s error=%s",
                    futures[future][:12],
                    exc,
                )

        if results:
            with self._lock:
                for result in results:
                    self._apply_result_locked(result)
                self._persist_locked()

    def _send_one(self, sid: str, item: dict, payload: str) -> dict:
        try:
            response = webpush(
                subscription_info={
                    "endpoint": item["endpoint"],
                    "keys": item["keys"],
                },
                data=payload,
                vapid_private_key=str(self._private_key_path),
                vapid_claims={"sub": _VAPID_SUBJECT},
                ttl=86400,
                timeout=10,
            )
            status = int(getattr(response, "status_code", 201))
            return {"sid": sid, "status": status, "retry_after": 0, "error": ""}
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            status = int(getattr(response, "status_code", 0) or 0)
            retry_after = self._retry_after_seconds(response)
            return {
                "sid": sid,
                "status": status,
                "retry_after": retry_after,
                "error": str(exc)[:500],
            }
        except Exception as exc:
            return {
                "sid": sid,
                "status": 0,
                "retry_after": 0,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }

    def _apply_result_locked(self, result: dict) -> None:
        sid = result["sid"]
        item = self._subscriptions.get(sid)
        if item is None:
            return
        status = int(result.get("status", 0))
        if 200 <= status < 300:
            item["last_success_at"] = _now_iso()
            item["last_error"] = ""
            item["failure_count"] = 0
            item["next_retry_at"] = 0
            return
        if status in (404, 410):
            del self._subscriptions[sid]
            self._presence.pop(item.get("device_id", ""), None)
            logger.info("推送端点确认失效，已自动清理 subscription=%s", sid[:12])
            return

        failures = int(item.get("failure_count", 0) or 0) + 1
        item["failure_count"] = failures
        item["last_error"] = result.get("error", "")
        item["updated_at"] = _now_iso()
        if status == 429 or status >= 500 or status == 0:
            server_delay = int(result.get("retry_after", 0) or 0)
            backoff = min(3600, 30 * (2 ** min(failures - 1, 7)))
            item["next_retry_at"] = time.time() + max(server_delay, backoff)
            logger.warning(
                "推送服务暂时不可用，将保留订阅并退避 subscription=%s status=%s",
                sid[:12],
                status or "network",
            )
        elif status in (401, 403):
            item["next_retry_at"] = 0
            logger.error(
                "推送服务拒绝 VAPID 身份，已保留订阅 subscription=%s status=%s",
                sid[:12],
                status,
            )
        else:
            item["next_retry_at"] = 0
            logger.error(
                "推送请求被拒绝，已保留订阅 subscription=%s status=%s",
                sid[:12],
                status,
            )

    def _build_payload(self, persona_id: str, message: dict) -> str:
        with self._config.lock:
            persona = self._config.load_personas().get(persona_id)
            title = persona.name if persona else persona_id
            hide_content = bool(
                self._config.get("notifications", "hide_content", default=False)
            )
        body = "发来一条新消息" if hide_content else self._message_preview(message)
        payload = {
            "title": title,
            "body": body,
            "data": {
                "persona_id": persona_id,
                "message_timestamp": message.get("timestamp", ""),
            },
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _message_preview(message: dict) -> str:
        parts: list[str] = []
        labels = {
            "emoji": "[表情]",
            "image": "[图片]",
            "file": "[文件]",
            "voice": "[语音]",
        }
        for block in message.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]).strip())
            elif block.get("type") in labels:
                parts.append(labels[block["type"]])
        preview = "\n".join(part for part in parts if part).strip()
        return (preview or "发来一条新消息")[:180]

    # ---- Persistent state and VAPID identity ----

    def _load_subscriptions(self) -> None:
        path = self._subscriptions_path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"通知订阅文件无法读取: {path}") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise RuntimeError(f"通知订阅文件格式无效: {path}")
        subscriptions = raw.get("subscriptions", {})
        if not isinstance(subscriptions, dict):
            raise RuntimeError(f"通知订阅列表格式无效: {path}")
        self._subscriptions = subscriptions
        with self._lock:
            if self._remove_expired_locked():
                self._persist_locked()

    def _remove_expired_locked(self, *, now_ms: float | None = None) -> bool:
        now_ms = now_ms if now_ms is not None else time.time() * 1000
        changed = False
        for sid, item in list(self._subscriptions.items()):
            expiration = item.get("expiration_time")
            if isinstance(expiration, (int, float)) and expiration <= now_ms:
                del self._subscriptions[sid]
                self._presence.pop(item.get("device_id", ""), None)
                changed = True
                logger.info("推送订阅已到期，自动清理 subscription=%s", sid[:12])
        return changed

    def _persist_locked(self) -> None:
        path = self._subscriptions_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": 1, "subscriptions": self._subscriptions},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _ensure_vapid_key(self) -> str:
        path = self._private_key_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                private_key = serialization.load_pem_private_key(
                    path.read_bytes(),
                    password=None,
                )
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError(f"VAPID 私钥无法读取: {path}") from exc
            if (
                not isinstance(private_key, ec.EllipticCurvePrivateKey)
                or not isinstance(private_key.curve, ec.SECP256R1)
            ):
                raise RuntimeError(f"VAPID 私钥类型无效: {path}")
        else:
            private_key = ec.generate_private_key(ec.SECP256R1())
            pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
            try:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
                with os.fdopen(fd, "wb") as handle:
                    handle.write(pem)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

        raw_public = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        return base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode("ascii")

    @staticmethod
    def _retry_after_seconds(response) -> int:
        if response is None:
            return 0
        value = (getattr(response, "headers", {}) or {}).get("Retry-After", "")
        if not value:
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0, int(retry_at.timestamp() - time.time()))
            except (TypeError, ValueError, OverflowError):
                return 0
