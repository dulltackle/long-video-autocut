"""阶段控制台启动边界。"""

from __future__ import annotations

import os
import stat
from pathlib import Path


_MARKER = ".video-auto-editor-workspace.json"
_EXPECTED_MARKER = b'{"schema_version":"workspace.v1"}\n'
_REQUIRED_DIRECTORIES = (
    "delivery",
    "delivery.previous",
    "work",
    "work/cache",
    "work/runs",
    "work/tmp",
)
_REQUIRED_FILES = (_MARKER, "work/.workspace.lock")


class StartupError(RuntimeError):
    """启动请求没有满足独立工具的安全契约。"""


def _assert_not_symlink(path: Path) -> os.stat_result:
    try:
        status = path.lstat()
    except OSError as exc:
        raise StartupError(
            "受管工作区或其内容不存在或不可读取"
        ) from exc
    if stat.S_ISLNK(status.st_mode):
        raise StartupError("工作区路径及受管内容不得包含符号链接")
    return status


def _assert_owned(status: os.stat_result) -> None:
    if hasattr(os, "getuid") and status.st_uid != os.getuid():
        raise StartupError(
            "工作区及受管内容的所有权必须属于当前用户"
        )


def _assert_directory(path: Path) -> None:
    before = _assert_not_symlink(path)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise StartupError("受管工作区布局必须由目录组成") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise StartupError("受管工作区目录在验证期间发生变化")
    _assert_owned(opened)


def _read_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    before = _assert_not_symlink(path)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise StartupError(
                "受管工作区文件必须是身份稳定的单链接普通文件"
            )
        _assert_owned(opened)
        if opened.st_size > maximum_bytes:
            raise StartupError("受管工作区文件大小不符合固定布局")
        value = os.read(descriptor, maximum_bytes + 1)
        if len(value) != opened.st_size:
            raise StartupError("受管工作区文件在验证期间发生变化")
        return value
    except StartupError:
        raise
    except OSError as exc:
        raise StartupError(
            "受管工作区文件必须是单链接普通文件"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_workspace(candidate: str | os.PathLike[str]) -> Path:
    """规范化并验证一个既有受管 ``.autocut`` 工作区。"""

    supplied = Path(candidate).expanduser()
    _assert_not_symlink(supplied)
    normalized = supplied.absolute().resolve(strict=True)
    if normalized.suffix != ".autocut":
        raise StartupError("WORKSPACE 必须是以 .autocut 结尾的受管工作区")
    _assert_directory(normalized)
    for relative in _REQUIRED_DIRECTORIES:
        path = normalized / relative
        if not path.is_relative_to(normalized):
            raise StartupError("受管路径不得越出工作区")
        _assert_directory(path)
    for relative in _REQUIRED_FILES:
        path = normalized / relative
        if not path.is_relative_to(normalized):
            raise StartupError("受管路径不得越出工作区")
        value = _read_regular_file(path, maximum_bytes=128)
        if relative == _MARKER and value != _EXPECTED_MARKER:
            raise StartupError("工作区受管标记无效")
        if relative != _MARKER and value:
            raise StartupError("工作区锁文件必须为空")

    return normalized
