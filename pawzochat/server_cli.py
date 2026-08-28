# PawzoChat - Multi-platform LLM-powered chatbot
# Copyright (C) 2026  iwyxdxl
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Command-line lifecycle for a headless PawzoChat deployment."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import socket
import stat
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pawzochat.runtime import (
    RuntimeConfigurationError,
    RuntimeOptions,
    normalize_data_dir,
)


_DEFAULT_DATA_DIR = "/var/lib/pawzochat"
_DEFAULT_ENV_FILE = "/etc/pawzochat/server.env"
_ENV_KEYS = {
    "PAWZOCHAT_DATA_DIR",
    "PAWZOCHAT_BIND",
    "PAWZOCHAT_PORT",
    "PAWZOCHAT_PUBLIC_URL",
    "PAWZOCHAT_PROXY_HOPS",
    "TZ",
}


def load_server_environment_file(path: str | Path) -> None:
    """Load the simple KEY=VALUE deployment file without executing shell."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"cannot read server environment file: {env_path}") from exc
    timezone_loaded = False
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeError(
                f"invalid environment line {env_path}:{line_number}"
            )
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in _ENV_KEYS:
            raise RuntimeError(
                f"unsupported environment key {key!r} in {env_path}:{line_number}"
            )
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            timezone_loaded = timezone_loaded or key == "TZ"
    if timezone_loaded and hasattr(time, "tzset"):
        time.tzset()


def _data_dir_default() -> str:
    return os.environ.get("PAWZOCHAT_DATA_DIR", _DEFAULT_DATA_DIR)


def _set_server_environment(data_dir: str | Path) -> Path:
    path = normalize_data_dir(data_dir)
    os.environ["PAWZOCHAT_MODE"] = "server"
    os.environ["PAWZOCHAT_DATA_DIR"] = str(path)
    return path


def _read_password(*, password_stdin: bool, confirm: bool) -> str:
    if password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            raise ValueError("标准输入中没有管理员密码")
        return password
    if not sys.stdin.isatty():
        raise ValueError(
            "非交互终端请使用 --password-stdin 提供管理员密码"
        )
    password = getpass.getpass("管理员密码: ")
    if confirm:
        repeated = getpass.getpass("再次输入: ")
        if password != repeated:
            raise ValueError("两次输入的密码不一致")
    return password


def _runtime_from_args(args) -> RuntimeOptions:
    return RuntimeOptions.server(
        data_dir=args.data_dir,
        bind_host=args.bind,
        port=args.port,
        public_url=args.public_url,
        proxy_hops=args.proxy_hops,
    )


def _add_data_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir",
        default=_data_dir_default(),
        help=f"运行数据目录（默认 {_DEFAULT_DATA_DIR}）",
    )


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    _add_data_argument(parser)
    parser.add_argument(
        "--bind", default=os.environ.get("PAWZOCHAT_BIND", "127.0.0.1"),
        help="应用监听地址（默认 127.0.0.1）",
    )
    parser.add_argument(
        "--port", default=os.environ.get("PAWZOCHAT_PORT", "62000"),
        help="应用监听端口（默认 62000）",
    )
    parser.add_argument(
        "--public-url", default=os.environ.get("PAWZOCHAT_PUBLIC_URL", ""),
        help="外部可信 HTTPS 地址，例如 https://chat.example.com",
    )
    parser.add_argument(
        "--proxy-hops",
        default=os.environ.get("PAWZOCHAT_PROXY_HOPS", "1"),
        help="可信反向代理层数（默认 1）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pawzochat")
    subcommands = parser.add_subparsers(dest="command", required=True)
    server = subcommands.add_parser("server", help="无 GUI 服务器部署")
    actions = server.add_subparsers(dest="server_command", required=True)

    init_parser = actions.add_parser("init", help="初始化服务器数据与密钥")
    _add_data_argument(init_parser)
    init_parser.add_argument(
        "--password-stdin", action="store_true",
        help="从标准输入读取一行管理员密码",
    )

    passwd_parser = actions.add_parser("passwd", help="修改管理员密码")
    _add_data_argument(passwd_parser)
    passwd_parser.add_argument("--password-stdin", action="store_true")

    run_parser = actions.add_parser("run", help="以前台单进程方式运行")
    _add_runtime_arguments(run_parser)

    doctor_parser = actions.add_parser("doctor", help="检查服务器部署状态")
    _add_runtime_arguments(doctor_parser)
    doctor_parser.add_argument(
        "--skip-public-check", action="store_true",
        help="不请求外部 HTTPS 地址",
    )
    return parser


def _command_init(args) -> int:
    _set_server_environment(args.data_dir)
    from pawzochat.paths import INSTANCE_LOCK_PATH
    from pawzochat.server_setup import has_admin_password, initialize_server
    from pawzochat.utils.instance_lock import InstanceLock

    with InstanceLock(INSTANCE_LOCK_PATH):
        password = None
        if args.password_stdin or not has_admin_password():
            password = _read_password(
                password_stdin=args.password_stdin,
                confirm=not args.password_stdin,
            )
        result = initialize_server(password=password)
    print(f"服务器数据已初始化: {result['data_dir']}")
    print("下一步：配置 /etc/pawzochat/server.env 后启动 pawzochat.service")
    return 0


def _command_passwd(args) -> int:
    _set_server_environment(args.data_dir)
    from pawzochat.paths import INSTANCE_LOCK_PATH
    from pawzochat.server_setup import ensure_server_layout, set_admin_password
    from pawzochat.utils.instance_lock import InstanceLock

    with InstanceLock(INSTANCE_LOCK_PATH):
        password = _read_password(
            password_stdin=args.password_stdin,
            confirm=not args.password_stdin,
        )
        ensure_server_layout()
        set_admin_password(password)
    print("管理员密码已更新；现有登录会话已失效")
    return 0


def _command_run(args) -> int:
    runtime = _runtime_from_args(args)
    _set_server_environment(runtime.data_dir)

    from pawzochat.app import App
    from pawzochat.paths import INSTANCE_LOCK_PATH
    from pawzochat.server_setup import assert_server_initialized
    from pawzochat.utils.instance_lock import InstanceLock

    with InstanceLock(INSTANCE_LOCK_PATH):
        assert_server_initialized()
        app = App(runtime=runtime)
        try:
            app.start()
        except KeyboardInterrupt:
            app.shutdown()
        except Exception:
            app.shutdown()
            raise
    return 0


@dataclass(frozen=True)
class Diagnostic:
    level: str
    name: str
    detail: str


def _diagnose(runtime: RuntimeOptions, *, check_public: bool) -> list[Diagnostic]:
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from pawzochat.paths import (
        CONFIG_PATH,
        DATA_DIR,
        SESSION_KEY_PATH,
        VAPID_PRIVATE_KEY_PATH,
    )
    from pawzochat.utils.crypto import is_valid_password_hash

    diagnostics: list[Diagnostic] = []

    if DATA_DIR.is_dir() and os.access(DATA_DIR, os.R_OK | os.W_OK | os.X_OK):
        mode = stat.S_IMODE(DATA_DIR.stat().st_mode)
        level = "WARN" if mode & 0o077 else "OK"
        diagnostics.append(Diagnostic(level, "数据目录", f"{DATA_DIR} mode={mode:04o}"))
    else:
        diagnostics.append(Diagnostic("FAIL", "数据目录", f"不可读写: {DATA_DIR}"))

    try:
        import yaml

        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        password = ((raw or {}).get("web") or {}).get("password", "")
        if not is_valid_password_hash(str(password)):
            raise ValueError("管理员密码未配置为安全哈希")
        diagnostics.append(Diagnostic("OK", "应用配置", str(CONFIG_PATH)))
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        diagnostics.append(Diagnostic("FAIL", "应用配置", str(exc)))

    try:
        key = SESSION_KEY_PATH.read_bytes()
        if len(key) != 32:
            raise ValueError("会话密钥长度无效")
        diagnostics.append(Diagnostic("OK", "会话密钥", "已持久化"))
    except (OSError, ValueError) as exc:
        diagnostics.append(Diagnostic("FAIL", "会话密钥", str(exc)))

    try:
        vapid = serialization.load_pem_private_key(
            VAPID_PRIVATE_KEY_PATH.read_bytes(), password=None,
        )
        if (
            not isinstance(vapid, ec.EllipticCurvePrivateKey)
            or not isinstance(vapid.curve, ec.SECP256R1)
        ):
            raise ValueError("VAPID 私钥不是 P-256 EC 密钥")
        diagnostics.append(Diagnostic("OK", "Web Push", "VAPID 私钥有效"))
    except (OSError, TypeError, ValueError, UnsupportedAlgorithm) as exc:
        diagnostics.append(Diagnostic("FAIL", "Web Push", str(exc)))

    target_host = runtime.bind_host
    if target_host in {"0.0.0.0", "::"}:
        diagnostics.append(Diagnostic(
            "WARN", "监听范围",
            f"{target_host} 会直接监听所有网卡；systemd 部署建议使用 127.0.0.1",
        ))
        target_host = "127.0.0.1" if target_host == "0.0.0.0" else "::1"
    else:
        diagnostics.append(Diagnostic(
            "OK", "监听范围", f"{runtime.bind_host}:{runtime.port}",
        ))

    try:
        with socket.create_connection((target_host, runtime.port), timeout=2):
            pass
        diagnostics.append(Diagnostic("OK", "服务进程", "监听端口可连接"))
    except OSError as exc:
        diagnostics.append(Diagnostic("FAIL", "服务进程", f"监听端口不可连接: {exc}"))

    if check_public:
        request = Request(
            f"{runtime.public_url}/healthz",
            headers={"User-Agent": "PawzoChat-Doctor/1"},
        )
        try:
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status != 200 or payload.get("mode") != "server":
                    raise ValueError("外部地址没有返回 PawzoChat server 健康状态")
            diagnostics.append(Diagnostic("OK", "公网 HTTPS", runtime.public_url))
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            diagnostics.append(Diagnostic("FAIL", "公网 HTTPS", str(exc)))
    else:
        diagnostics.append(Diagnostic("WARN", "公网 HTTPS", "已跳过外部检查"))

    now = datetime.now().astimezone()
    diagnostics.append(Diagnostic(
        "OK", "进程时区", f"{now.tzname()} {now.strftime('%z')}",
    ))
    return diagnostics


def _command_doctor(args) -> int:
    runtime = _runtime_from_args(args)
    _set_server_environment(runtime.data_dir)
    diagnostics = _diagnose(runtime, check_public=not args.skip_public_check)
    for item in diagnostics:
        print(f"[{item.level}] {item.name}: {item.detail}")
    return 1 if any(item.level == "FAIL" for item in diagnostics) else 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    try:
        env_file = os.environ.get("PAWZOCHAT_ENV_FILE", _DEFAULT_ENV_FILE)
        load_server_environment_file(env_file)
        parser = build_parser()
        args = parser.parse_args(argv)
        if args.server_command == "init":
            return _command_init(args)
        if args.server_command == "passwd":
            return _command_passwd(args)
        if args.server_command == "run":
            return _command_run(args)
        if args.server_command == "doctor":
            return _command_doctor(args)
    except (RuntimeConfigurationError, RuntimeError, ValueError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    parser.error("unknown server command")
    return 2
