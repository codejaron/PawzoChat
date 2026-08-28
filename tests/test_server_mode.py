from __future__ import annotations

import os
from pathlib import Path
import json
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from pawzochat.runtime import (
    RuntimeConfigurationError,
    RuntimeOptions,
    normalize_data_dir,
    normalize_https_origin,
)
from pawzochat.server_cli import load_server_environment_file
from pawzochat.utils.crypto import hash_password, is_valid_password_hash, verify_password
from pawzochat.utils.instance_lock import InstanceAlreadyRunning, InstanceLock
from pawzochat.utils.port_lock import ensure_listen_port_free
from pawzochat.web.access import AccessMode, AccessModeMiddleware
from pawzochat.web.login_limiter import LoginRateLimiter
from pawzochat.web.session_key import (
    load_or_create_session_key,
    rotate_session_key,
)


class RuntimeOptionsTests(unittest.TestCase):
    def test_server_runtime_normalizes_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeOptions.server(
                data_dir=tmp,
                bind_host="127.0.0.1",
                port="62000",
                public_url="https://CHAT.Example.com/",
                proxy_hops="1",
            )
        self.assertTrue(runtime.is_server)
        self.assertEqual(runtime.public_url, "https://chat.example.com")
        self.assertEqual(runtime.port, 62000)
        self.assertEqual(runtime.proxy_hops, 1)

    def test_public_url_rejects_paths_and_plain_http(self):
        self.assertIsNone(normalize_https_origin("http://chat.example.com"))
        self.assertIsNone(normalize_https_origin("https://chat.example.com/app"))

    def test_data_dir_rejects_filesystem_root(self):
        with self.assertRaises(RuntimeConfigurationError):
            normalize_data_dir(Path("/"))

    def test_environment_file_is_parsed_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.env"
            path.write_text(
                "PAWZOCHAT_PORT=62123\n"
                "PAWZOCHAT_PUBLIC_URL='https://chat.example.com'\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                load_server_environment_file(path)
                self.assertEqual(os.environ["PAWZOCHAT_PORT"], "62123")
                self.assertEqual(
                    os.environ["PAWZOCHAT_PUBLIC_URL"],
                    "https://chat.example.com",
                )


class LockingTests(unittest.TestCase):
    def test_instance_lock_rejects_second_process_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pawzochat.lock"
            first = InstanceLock(path)
            second = InstanceLock(path)
            first.acquire()
            try:
                with self.assertRaises(InstanceAlreadyRunning):
                    second.acquire()
            finally:
                first.release()

    def test_server_port_check_never_terminates_owner(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaises(OSError):
                ensure_listen_port_free(
                    port,
                    "127.0.0.1",
                    label="test",
                    terminate_existing=False,
                )
            self.assertGreater(listener.fileno(), -1)


class KeyAndLimiterTests(unittest.TestCase):
    def test_password_hash_validation_rejects_malformed_or_excessive_work(self):
        password_hash = hash_password("StrongPass123")
        self.assertTrue(is_valid_password_hash(password_hash))
        self.assertTrue(verify_password("StrongPass123", password_hash))
        malformed = "$pbkdf2-sha256$999999999$00$00"
        self.assertFalse(is_valid_password_hash(malformed))
        self.assertFalse(verify_password("StrongPass123", malformed))

    def test_session_key_persists_and_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth" / "session.key"
            first = load_or_create_session_key(path)
            self.assertEqual(load_or_create_session_key(path), first)
            second = rotate_session_key(path)
            self.assertNotEqual(second, first)
            self.assertEqual(len(second), 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_login_limiter_is_per_client(self):
        limiter = LoginRateLimiter(max_failures=2, window_seconds=60)
        self.assertEqual(limiter.record_failure("client-a"), 0)
        self.assertGreater(limiter.record_failure("client-a"), 0)
        self.assertEqual(limiter.retry_after("client-b"), 0)
        limiter.record_success("client-a")
        self.assertEqual(limiter.retry_after("client-a"), 0)


class _DummyConfig:
    def __init__(self, password: str):
        self._data = {
            "web": {
                "password": password,
                "public_enabled": False,
                "public_port": 0,
                "public_secret": "",
                "reverse_proxy_enabled": False,
                "public_base_url": "",
                "port": 62000,
            },
            "chat": {},
            "reply": {},
            "theme": {},
            "notifications": {},
        }
        self.lock = threading.RLock()

    @property
    def data(self):
        return self._data

    def get(self, *keys, default=None):
        current = self._data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
        return current

    def save(self):
        return None


class _DummyApp:
    def __init__(self, runtime, password):
        self.runtime = runtime
        self.config = _DummyConfig(password)
        self.updater = None
        self.web_push_service = None


def _csrf_token(response) -> str:
    match = re.search(
        rb'name="csrf_token"\s+value="([a-f0-9]+)"', response.data,
    )
    if not match:
        raise AssertionError("login response did not contain a CSRF token")
    return match.group(1).decode("ascii")


class ServerAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = RuntimeOptions.server(
            data_dir=self.tmp.name,
            bind_host="127.0.0.1",
            port=62000,
            public_url="https://chat.example.com",
            proxy_hops=1,
        )
        dummy = _DummyApp(self.runtime, hash_password("StrongPass123"))
        session_path = Path(self.tmp.name) / "auth" / "session.key"
        with patch("pawzochat.web.app.SESSION_KEY_PATH", session_path):
            from pawzochat.web.app import create_app

            self.app = create_app(dummy)
        self.app.wsgi_app = AccessModeMiddleware(
            self.app.wsgi_app, AccessMode.SERVER_ADMIN,
        )

        @self.app.get("/_protected_test")
        def protected_test():
            return {"ok": True}

        @self.app.post("/_write_test")
        def write_test():
            return {"ok": True}

        self.client = self.app.test_client()
        self.base_url = "https://chat.example.com"

    def tearDown(self):
        self.tmp.cleanup()

    def _login(self, password="StrongPass123"):
        login_page = self.client.get("/login", base_url=self.base_url)
        return self.client.post(
            "/login",
            base_url=self.base_url,
            data={"csrf_token": _csrf_token(login_page), "password": password},
        )

    def test_server_requires_login_and_accepts_admin_password(self):
        response = self.client.get("/_protected_test", base_url=self.base_url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")

        response = self._login()
        self.assertEqual(response.status_code, 302)
        response = self.client.get("/_protected_test", base_url=self.base_url)
        self.assertEqual(response.status_code, 200)

    def test_health_check_is_public_but_api_is_not(self):
        health = self.client.get("/healthz", base_url=self.base_url)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json, {"mode": "server", "status": "ok"})
        api = self.client.get("/api/status", base_url=self.base_url)
        self.assertEqual(api.status_code, 401)

    def test_authenticated_writes_require_same_origin(self):
        self.assertEqual(self._login().status_code, 302)
        denied = self.client.post("/_write_test", base_url=self.base_url)
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.post(
            "/_write_test",
            base_url=self.base_url,
            headers={"Origin": self.base_url},
        )
        self.assertEqual(allowed.status_code, 200)

    def test_server_settings_are_read_only_deployment_metadata(self):
        self.assertEqual(self._login().status_code, 302)
        settings = self.client.get("/api/settings", base_url=self.base_url)
        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.json["deployment_mode"], "server")
        self.assertEqual(settings.json["server"]["public_url"], self.base_url)
        denied = self.client.patch(
            "/api/settings",
            base_url=self.base_url,
            headers={"Origin": self.base_url},
            json={"web": {"public_enabled": True}},
        )
        self.assertEqual(denied.status_code, 403)


class ServerInitIntegrationTests(unittest.TestCase):
    def test_init_uses_external_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "runtime"
            result = subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "server",
                    "init",
                    "--data-dir",
                    str(data_dir),
                    "--password-stdin",
                ],
                input="StrongPass123\n",
                text=True,
                capture_output=True,
                cwd=Path(__file__).resolve().parents[1],
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((data_dir / "config" / "config.yaml").is_file())
            self.assertTrue((data_dir / "auth" / "session.key").is_file())
            self.assertTrue((data_dir / "push" / "vapid_private_key.pem").is_file())
            self.assertTrue((data_dir / "emoji").is_dir())

    def test_paths_honor_server_data_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PAWZOCHAT_DATA_DIR"] = tmp
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pawzochat.paths import DATA_DIR; print(DATA_DIR)",
                ],
                text=True,
                capture_output=True,
                env=env,
                cwd=Path(__file__).resolve().parents[1],
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(result.stdout.strip()), Path(tmp).resolve())

    def test_quick_setup_uses_external_mcp_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            script = data_dir / "mcp_servers" / "web_search_pawapi" / "server.py"
            script.parent.mkdir(parents=True)
            script.touch()
            env = os.environ.copy()
            env["PAWZOCHAT_DATA_DIR"] = tmp
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json; "
                    "from pawzochat.web.routes.api_setup import _script_stdio_config; "
                    "print(json.dumps(_script_stdio_config('web_search_pawapi')))",
                ],
                text=True,
                capture_output=True,
                env=env,
                cwd=Path(__file__).resolve().parents[1],
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            config = json.loads(result.stdout)
            self.assertEqual(config["command"], sys.executable)
            self.assertEqual(config["args"], [str(script.resolve())])


if __name__ == "__main__":
    unittest.main()
