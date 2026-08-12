"""``python -m tools.live_progress`` 唯一启动入口。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from .build import BuildError, ensure_web_build, validate_web_toolchain
from .server import create_http_server
from .startup import StartupError, validate_workspace


_LOOPBACK_HOST = "127.0.0.1"
_WIREGUARD_HOST = "10.8.0.5"
_WIREGUARD_CLIENT = "10.8.0.3"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.live_progress",
        description="打开指定受管工作区的只读阶段控制台",
    )
    parser.add_argument("WORKSPACE", help="单个既有受管 .autocut 工作区")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="由同一入口监管 Vite 开发服务器",
    )
    parser.add_argument(
        "--listen",
        choices=("loopback", "wireguard"),
        default="loopback",
        help="监听边界（默认：loopback）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=4173,
        help="前端端口（默认：4173）",
    )
    return parser


def _serve_daily(workspace: Path, web_root: Path, listen: str, port: int) -> int:
    host = _LOOPBACK_HOST if listen == "loopback" else _WIREGUARD_HOST
    allowed_client = None if listen == "loopback" else _WIREGUARD_CLIENT
    distribution = ensure_web_build(web_root)
    server = create_http_server(
        workspace,
        distribution,
        host=host,
        port=port,
        allowed_client=allowed_client,
    )
    print(f"阶段控制台：http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _serve_development(workspace: Path, web_root: Path, listen: str, port: int) -> int:
    validate_web_toolchain(web_root)
    api_server = create_http_server(workspace, web_root, host=_LOOPBACK_HOST, port=0)
    api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
    api_thread.start()
    host = _LOOPBACK_HOST if listen == "loopback" else _WIREGUARD_HOST
    if listen == "wireguard":
        print(
            "警告：WireGuard 开发模式依赖只有可信开发对端可达 "
            "10.8.0.5；"
            "Vite 不实施应用层来源限制。",
            file=sys.stderr,
            flush=True,
        )
    environment = os.environ.copy()
    environment["LIVE_PROGRESS_API_ORIGIN"] = (
        f"http://{_LOOPBACK_HOST}:{api_server.server_port}"
    )
    process = subprocess.Popen(
        (
            "npm",
            "run",
            "dev",
            "--",
            "--host",
            host,
            "--port",
            str(port),
            "--strictPort",
        ),
        cwd=web_root,
        env=environment,
    )
    print(f"阶段控制台开发服务：http://{host}:{port}", flush=True)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()
    finally:
        api_server.shutdown()
        api_server.server_close()
        api_thread.join(timeout=2)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    options = parser.parse_args(arguments)
    try:
        workspace = validate_workspace(options.WORKSPACE)
        if not 1 <= options.port <= 65535:
            raise StartupError("端口必须位于 1 到 65535")
        web_root = Path(__file__).resolve().parent / "web"
        if options.dev:
            return _serve_development(
                workspace, web_root, options.listen, options.port
            )
        return _serve_daily(workspace, web_root, options.listen, options.port)
    except (StartupError, BuildError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
