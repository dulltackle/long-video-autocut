"""同源前端与版本化只读 API。"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import stat
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..startup import validate_workspace
from .observation import RUN_ID_PATTERN, SCHEMA_VERSION, discover_runs, project_run


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _etag(body: bytes) -> str:
    return f'"sha256:{hashlib.sha256(body).hexdigest()}"'


def _safe_static_file(static_root: Path, request_path: str) -> Path | None:
    decoded = unquote(request_path)
    parts = tuple(part for part in decoded.split("/") if part)
    if any(part in {".", ".."} or part.startswith(".") for part in parts):
        return None
    candidate = static_root.joinpath(*parts) if parts else static_root / "index.html"
    try:
        status = candidate.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_relative_to(static_root) or status.st_uid != os.getuid():
        return None
    return resolved


def _workspace_identity(workspace_root: Path) -> tuple[int, int]:
    status = workspace_root.lstat()
    return status.st_dev, status.st_ino


def create_http_server(
    workspace: str | os.PathLike[str],
    static_root: str | os.PathLike[str],
    *,
    host: str = "127.0.0.1",
    port: int = 4173,
    allowed_client: str | None = None,
) -> ThreadingHTTPServer:
    """创建只绑定调用方明确地址的阶段控制台服务。"""

    workspace_root = validate_workspace(workspace)
    expected_workspace_identity = _workspace_identity(workspace_root)
    supplied_web_root = Path(static_root).absolute()
    if supplied_web_root.is_symlink():
        raise ValueError("前端构建目录不能是符号链接")
    web_root = supplied_web_root.resolve(strict=True)
    if not web_root.is_dir():
        raise ValueError("前端构建目录必须是普通目录")

    class ReadOnlyHandler(BaseHTTPRequestHandler):
        server_version = "LiveProgress/1"

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _client_allowed(self) -> bool:
            if allowed_client is None or self.client_address[0] == allowed_client:
                return True
            self.send_error(HTTPStatus.FORBIDDEN)
            return False

        def _write_json(
            self,
            status: HTTPStatus,
            payload: object,
            *,
            allow_not_modified: bool = False,
        ) -> None:
            body = _json_bytes(payload)
            etag = _etag(body)
            if allow_not_modified and self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _error(
            self, status: HTTPStatus, code: str, message: str
        ) -> None:
            self._write_json(
                status,
                {
                    "schema_version": SCHEMA_VERSION,
                    "error": {"code": code, "message": message},
                },
            )

        def _snapshots(self) -> dict[str, tuple[dict[str, object], bytes, str]]:
            if _workspace_identity(workspace_root) != expected_workspace_identity:
                raise OSError("workspace identity changed")
            snapshots: dict[str, tuple[dict[str, object], bytes, str]] = {}
            for run_id in discover_runs(workspace_root):
                payload = project_run(workspace_root, run_id)
                body = _json_bytes(payload)
                snapshots[run_id] = (payload, body, _etag(body))
            return snapshots

        def do_GET(self) -> None:
            if not self._client_allowed():
                return
            path = urlsplit(self.path).path
            if path == "/api/v1/runs":
                try:
                    snapshots = self._snapshots()
                except Exception:
                    self._error(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "internal_error",
                        "工作区安全状态已变化，无法继续读取",
                    )
                    return
                items = []
                for run_id, (snapshot, _body, snapshot_etag) in snapshots.items():
                    observation = snapshot["observation"]
                    items.append(
                        {
                            "run_id": run_id,
                            "observation_state": observation["state"],
                            "started_at": observation["started_at"],
                            "stage": observation["stage"],
                            "duration_ms": observation["duration_ms"],
                            "last_event_at": observation["last_event_at"],
                            "diagnostics": snapshot["diagnostics"],
                            "snapshot_etag": snapshot_etag,
                        }
                    )
                items.sort(
                    key=lambda item: (
                        item["started_at"]["status"] != "available",
                        item["run_id"],
                    )
                )
                available = [
                    item for item in items if item["started_at"]["status"] == "available"
                ]
                unavailable = [
                    item for item in items if item["started_at"]["status"] != "available"
                ]
                available.sort(key=lambda item: item["run_id"])
                available.sort(
                    key=lambda item: item["started_at"]["value"], reverse=True
                )
                self._write_json(
                    HTTPStatus.OK,
                    {"schema_version": SCHEMA_VERSION, "runs": [*available, *unavailable]},
                    allow_not_modified=True,
                )
                return
            run_prefix = "/api/v1/runs/"
            if path.startswith(run_prefix):
                run_id = unquote(path.removeprefix(run_prefix))
                if "/" in run_id or RUN_ID_PATTERN.fullmatch(run_id) is None:
                    self._error(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_request",
                        "直播拆条运行标识格式无效",
                    )
                    return
                try:
                    snapshots = self._snapshots()
                except Exception:
                    self._error(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "internal_error",
                        "工作区安全状态已变化，无法继续读取",
                    )
                    return
                selected = snapshots.get(run_id)
                if selected is None:
                    self._error(
                        HTTPStatus.NOT_FOUND,
                        "run_not_found",
                        "未找到指定的直播拆条运行。",
                    )
                    return
                payload, _body, _selected_etag = selected
                self._write_json(
                    HTTPStatus.OK,
                    payload,
                    allow_not_modified=True,
                )
                return
            if path.startswith("/api/"):
                self._error(
                    HTTPStatus.NOT_FOUND,
                    "resource_not_found",
                    "请求的只读资源不存在",
                )
                return
            selected = _safe_static_file(web_root, path)
            if selected is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = selected.read_bytes()
            content_type, _ = mimetypes.guess_type(selected.name)
            if selected.suffix == ".html":
                content_type = "text/html; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            self._method_not_allowed()

        def do_PUT(self) -> None:
            self._method_not_allowed()

        def do_PATCH(self) -> None:
            self._method_not_allowed()

        def do_DELETE(self) -> None:
            self._method_not_allowed()

        def _method_not_allowed(self) -> None:
            if not self._client_allowed():
                return
            if urlsplit(self.path).path.startswith("/api/"):
                self._error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "该资源只允许只读访问",
                )
            else:
                self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    return ThreadingHTTPServer((host, port), ReadOnlyHandler)
