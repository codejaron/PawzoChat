# PawzoChat - Multi-platform LLM-powered chatbot
# Copyright (C) 2026  iwyxdxl
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Top-level command dispatch without importing runtime paths too early."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] == "server":
        from pawzochat.server_cli import main as server_main
        return server_main(args)

    if args and args[0] == "desktop":
        args.pop(0)

    if "--apply-update" in sys.argv:
        from pawzochat.updater import apply_update
        apply_update(sys.argv)
        return 0

    if args:
        print(f"未知参数: {' '.join(args)}", file=sys.stderr)
        print("可用命令: desktop | server init|run|doctor|passwd", file=sys.stderr)
        return 2

    from pawzochat.updater import cleanup_staging
    cleanup_staging()

    from pawzochat.app import App
    from pawzochat.runtime import RuntimeOptions

    app = App(runtime=RuntimeOptions.desktop())
    try:
        app.start()
    except KeyboardInterrupt:
        app.shutdown()
    return 0
