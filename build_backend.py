"""在 setuptools 构建前后执行发布结构门禁。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.validate_architecture import (
    validate_source_tree,
    validate_wheel,
)

_PROJECT_ROOT = Path(__file__).resolve().parent


class UnsupportedOperation(RuntimeError):
    """构建前端请求了本项目不发布的产物类型。"""


def _setuptools_backend() -> Any:
    from setuptools import build_meta  # type: ignore[import-untyped]

    return build_meta


def get_requires_for_build_wheel(
    config_settings: dict[str, Any] | None = None,
) -> list[str]:
    return _setuptools_backend().get_requires_for_build_wheel(config_settings)


def prepare_metadata_for_build_wheel(
    metadata_directory: str,
    config_settings: dict[str, Any] | None = None,
) -> str:
    validate_source_tree(_PROJECT_ROOT)
    return _setuptools_backend().prepare_metadata_for_build_wheel(
        metadata_directory,
        config_settings,
    )


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    validate_source_tree(_PROJECT_ROOT)
    backend = _setuptools_backend()
    if metadata_directory is None:
        wheel_name = backend.build_wheel(wheel_directory, config_settings)
    else:
        wheel_name = backend.build_wheel(
            wheel_directory,
            config_settings,
            metadata_directory,
        )

    wheel_path = Path(wheel_directory) / wheel_name
    try:
        validate_wheel(wheel_path, source_root=_PROJECT_ROOT)
    except Exception:
        wheel_path.unlink(missing_ok=True)
        raise
    return wheel_name


def get_requires_for_build_editable(
    config_settings: dict[str, Any] | None = None,
) -> list[str]:
    return _setuptools_backend().get_requires_for_build_editable(
        config_settings
    )


def prepare_metadata_for_build_editable(
    metadata_directory: str,
    config_settings: dict[str, Any] | None = None,
) -> str:
    validate_source_tree(_PROJECT_ROOT)
    return _setuptools_backend().prepare_metadata_for_build_editable(
        metadata_directory,
        config_settings,
    )


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    validate_source_tree(_PROJECT_ROOT)
    backend = _setuptools_backend()
    if metadata_directory is None:
        return backend.build_editable(wheel_directory, config_settings)
    return backend.build_editable(
        wheel_directory,
        config_settings,
        metadata_directory,
    )


def get_requires_for_build_sdist(
    config_settings: dict[str, Any] | None = None,
) -> list[str]:
    del config_settings
    return []


def build_sdist(
    sdist_directory: str,
    config_settings: dict[str, Any] | None = None,
) -> str:
    del sdist_directory, config_settings
    raise UnsupportedOperation(
        "本项目仅支持构建经过结构门禁的 wheel 发布产物"
    )
