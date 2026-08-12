"""可复现前端构建的身份与复用策略。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path


CommandRunner = Callable[[tuple[str, ...], Path], str]
_MARKER_NAME = ".live-progress-build.json"
_ROOT_INPUTS = (
    "index.html",
    "package.json",
    "package-lock.json",
    "eslint.config.js",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
    "vite.config.ts",
)


class BuildError(RuntimeError):
    """前端依赖或可复现构建契约未满足。"""


def _run(command: tuple[str, ...], cwd: Path) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        details = getattr(exc, "stderr", "") or ""
        message = f"前端命令失败：{' '.join(command)}\n{details.strip()}"
        raise BuildError(message) from exc
    return completed.stdout


def _major(version: str, tool: str) -> int:
    matched = re.fullmatch(r"v?(\d+)(?:\.\d+){0,2}\s*", version)
    if matched is None:
        raise BuildError(f"无法识别 {tool} 版本")
    return int(matched.group(1))


def _input_files(web_root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for name in _ROOT_INPUTS:
        candidate = web_root / name
        if candidate.is_file():
            files.append(candidate)
    for directory_name in ("src", "public"):
        directory = web_root / directory_name
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    return tuple(sorted(files, key=lambda path: path.relative_to(web_root).as_posix()))


def _build_identity(web_root: Path, node_major: int) -> str:
    digest = hashlib.sha256()
    digest.update(f"node-major:{node_major}\0".encode())
    files = _input_files(web_root)
    if not files:
        raise BuildError("前端工程没有可构建输入")
    for path in files:
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise BuildError(
                "前端构建输入必须是普通文件且不能是符号链接"
            )
        digest.update(path.relative_to(web_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _read_marker(marker: Path) -> str | None:
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if isinstance(value, dict) and set(value) == {"build_identity"}:
        identity = value["build_identity"]
        return identity if isinstance(identity, str) else None
    return None


def _write_marker(marker: Path, identity: str) -> None:
    temporary = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {"build_identity": identity},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker)


def ensure_web_build(
    web_root: str | os.PathLike[str], *, run: CommandRunner = _run
) -> Path:
    """构建或复用与全部输入内容匹配的本地前端。"""

    root = Path(web_root).absolute()
    if not (root / "node_modules").is_dir():
        raise BuildError(
            "前端依赖尚未安装；请先执行 " f"cd {root} && npm ci"
        )
    node_major = validate_web_toolchain(root, run=run)

    identity = _build_identity(root, node_major)
    distribution = root / "dist"
    marker = root / _MARKER_NAME
    if (
        distribution.is_dir()
        and (distribution / "index.html").is_file()
        and _read_marker(marker) == identity
    ):
        return distribution

    run(("npm", "run", "build"), root)
    if not distribution.is_dir() or not (distribution / "index.html").is_file():
        raise BuildError("前端构建没有形成 dist/index.html")
    _write_marker(marker, identity)
    return distribution


def validate_web_toolchain(
    web_root: str | os.PathLike[str], *, run: CommandRunner = _run
) -> int:
    """验证显式安装的前端依赖和固定工具链，返回 Node 主版本。"""

    root = Path(web_root).absolute()
    if not (root / "node_modules").is_dir():
        raise BuildError(
            "前端依赖尚未安装；请先执行 "
            f"cd {root} && npm ci"
        )
    node_major = _major(run(("node", "--version"), root), "Node")
    if node_major != 24:
        raise BuildError("阶段控制台要求 Node 24")
    npm_major = _major(run(("npm", "--version"), root), "npm")
    if npm_major != 11:
        raise BuildError("阶段控制台要求 npm 11")
    return node_major
