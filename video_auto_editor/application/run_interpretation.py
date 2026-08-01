"""skill 调度器的机器清单解释边界。

本模块只投影已经由 CLI 底座形成的 ``run.json`` 与
``delivery/manifest.json``。它不接收终端文本或进程退出码，也不重做
媒体处理、主题判定、导出选择或交付判断。
"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path
from typing import Any

from video_auto_editor.clip_planning import ResultKind
from video_auto_editor.delivery.schema import (
    DELIVERY_MANIFEST_SCHEMA_VERSION,
)
from video_auto_editor.delivery.verification import (
    DeliveryManifestReader,
    DeliveryManifestReadReason,
    DeliveryManifestReadResult,
    DeliveryManifestReadState,
    DeliveryManifestSummary,
)
from video_auto_editor.diagnostics import (
    DiagnosticPackageReader,
    DiagnosticPackageSnapshot,
)
from video_auto_editor.runtime.identity import RunId

RunInterpretation = dict[str, Any]


class _ManifestSymlink(RuntimeError):
    pass


def interpret_run(
    workspace_dir: str | Path,
    run_id: str | RunId,
) -> RunInterpretation:
    """仅从指定运行的诊断清单与关联交付清单解释结果。

    调用方必须传入已关联到本次 CLI 调用的 ``run_id``；本边界不通过
    扫描历史运行目录或解析终端文本猜测运行归属。
    """
    expected_run_id = RunId(str(run_id))
    workspace = Path(workspace_dir)
    run_parts = ("work", "runs", str(expected_run_id))
    try:
        events = (
            _read_managed_file(workspace, (*run_parts, "events.jsonl"))
            or b""
        )
    except _ManifestSymlink:
        return _interpret_corrupt_diagnostics(
            workspace,
            expected_run_id,
            reason="event_log_symlink",
        )
    except OSError:
        return _interpret_incomplete_diagnostics(
            workspace,
            expected_run_id,
            reason="event_log_unreadable",
        )
    try:
        manifest_bytes = _read_managed_file(
            workspace,
            (*run_parts, "run.json"),
        )
    except _ManifestSymlink:
        return _interpret_corrupt_diagnostics(
            workspace,
            expected_run_id,
            reason="manifest_symlink",
        )
    except OSError:
        return _interpret_incomplete_diagnostics(
            workspace,
            expected_run_id,
            reason="manifest_unreadable",
        )
    diagnostic_result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=events,
            manifest=manifest_bytes,
        )
    )

    if diagnostic_result.run_id not in {None, expected_run_id}:
        return _interpret_corrupt_diagnostics(
            workspace,
            expected_run_id,
            reason="run_id_directory_mismatch",
        )

    diagnostic_state = diagnostic_result.state.value
    diagnostic_reason = diagnostic_result.reason.value
    if diagnostic_state == "corrupt":
        return _interpret_corrupt_diagnostics(
            workspace,
            expected_run_id,
            reason=diagnostic_reason,
        )
    if diagnostic_state == "incomplete":
        return _interpret_incomplete_diagnostics(
            workspace,
            expected_run_id,
            reason=diagnostic_reason,
        )

    assert manifest_bytes is not None
    run_manifest = json.loads(manifest_bytes)
    lifecycle = run_manifest["lifecycle"]
    errors = run_manifest["errors"]
    if lifecycle["outcome"] == "succeeded":
        expected_result_kind = ResultKind(
            lifecycle["result_kind"]["value"]
        )
        delivery_result = _read_delivery_manifest(
            workspace,
            expected_run_id,
            expected_result_kind=expected_result_kind,
        )
        if delivery_result.state is not DeliveryManifestReadState.COMPLETE:
            return _unknown_run_interpretation(
                expected_run_id,
                diagnostic_state=diagnostic_state,
                diagnostic_reason=diagnostic_reason,
                delivery_result=delivery_result,
            )
        return _successful_run_interpretation(
            expected_run_id,
            diagnostic_state=diagnostic_state,
            diagnostic_reason=diagnostic_reason,
            exit_code=lifecycle["exit_code"],
            recovery_incomplete=errors["recovery_incomplete"],
            delivery_result=delivery_result,
        )

    interruption = lifecycle["interruption"]
    primary_error = errors["primary_error"]
    return {
        "run_id": str(expected_run_id),
        "diagnostics": {
            "state": diagnostic_state,
            "reason": diagnostic_reason,
        },
        "terminal_state": lifecycle["outcome"],
        "exit_code": lifecycle["exit_code"],
        "result_kind": None,
        "interruption_signal": (
            interruption["signal"]
            if interruption["status"] == "available"
            else None
        ),
        "primary_error": (
            _project_run_error(primary_error)
            if primary_error.get("status") != "not_applicable"
            else None
        ),
        "associated_errors": [
            _project_run_error(error)
            for error in errors["associated_errors"]
        ],
        "recovery_incomplete": errors["recovery_incomplete"],
        "delivery_manifest": {
            "state": "not_applicable",
            "reason": "terminal_not_succeeded",
        },
        "delivery": None,
    }


def _read_managed_file(
    workspace: Path,
    parts: tuple[str, ...],
) -> bytes | None:
    """通过逐级 ``openat`` 读取受管文件，全程不跟随符号链接。"""
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_descriptors: list[int] = []
    file_descriptor: int | None = None
    try:
        try:
            root_descriptor = os.open(workspace, directory_flags)
        except FileNotFoundError:
            return None
        except OSError as failure:
            _raise_if_unsafe_path(failure)
            raise
        directory_descriptors.append(root_descriptor)

        for component in parts[:-1]:
            try:
                descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_descriptors[-1],
                )
            except FileNotFoundError:
                return None
            except OSError as failure:
                _raise_if_unsafe_path(failure)
                raise
            directory_descriptors.append(descriptor)

        try:
            file_descriptor = os.open(
                parts[-1],
                file_flags,
                dir_fd=directory_descriptors[-1],
            )
        except FileNotFoundError:
            return None
        except OSError as failure:
            _raise_if_unsafe_path(failure)
            raise
        if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
            raise _ManifestSymlink
        stream = os.fdopen(file_descriptor, "rb")
        file_descriptor = None
        with stream:
            return stream.read()
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        for descriptor in reversed(directory_descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _raise_if_unsafe_path(failure: OSError) -> None:
    if failure.errno in {errno.ELOOP, errno.ENOTDIR}:
        raise _ManifestSymlink from None


def _read_delivery_manifest(
    workspace: Path,
    run_id: RunId,
    *,
    expected_result_kind: ResultKind | None = None,
) -> DeliveryManifestReadResult:
    try:
        manifest = _read_managed_file(
            workspace,
            ("delivery", "manifest.json"),
        )
    except _ManifestSymlink:
        return DeliveryManifestReadResult(
            DeliveryManifestReadState.CORRUPT,
            DeliveryManifestReadReason.MANIFEST_SYMLINK,
            None,
        )
    except OSError:
        return DeliveryManifestReadResult(
            DeliveryManifestReadState.INCOMPLETE,
            DeliveryManifestReadReason.MANIFEST_UNREADABLE,
            None,
        )
    return DeliveryManifestReader.read(
        manifest,
        expected_run_id=run_id,
        expected_result_kind=expected_result_kind,
    )


def _interpret_incomplete_diagnostics(
    workspace: Path,
    run_id: RunId,
    *,
    reason: str,
) -> RunInterpretation:
    return _interpret_unresolved_diagnostics(
        workspace,
        run_id,
        state="incomplete",
        reason=reason,
    )


def _interpret_corrupt_diagnostics(
    workspace: Path,
    run_id: RunId,
    *,
    reason: str,
) -> RunInterpretation:
    return _interpret_unresolved_diagnostics(
        workspace,
        run_id,
        state="corrupt",
        reason=reason,
    )


def _interpret_unresolved_diagnostics(
    workspace: Path,
    run_id: RunId,
    *,
    state: str,
    reason: str,
) -> RunInterpretation:
    delivery_result = _read_delivery_manifest(workspace, run_id)
    if delivery_result.state is DeliveryManifestReadState.COMPLETE:
        return _successful_run_interpretation(
            run_id,
            diagnostic_state=state,
            diagnostic_reason=reason,
            exit_code=None,
            recovery_incomplete=None,
            delivery_result=delivery_result,
        )
    return _unknown_run_interpretation(
        run_id,
        diagnostic_state=state,
        diagnostic_reason=reason,
        delivery_result=delivery_result,
    )


def _project_run_error(error: dict[str, Any]) -> dict[str, Any]:
    return {
        "error_code": error["error_code"],
        "safe_message": error["safe_message"],
        "retryable_in_new_run": error["retryable_in_new_run"],
        "operator_action": error["operator_action"],
    }


def _successful_run_interpretation(
    run_id: RunId,
    *,
    diagnostic_state: str,
    diagnostic_reason: str,
    exit_code: int | None,
    recovery_incomplete: bool | None,
    delivery_result: DeliveryManifestReadResult,
) -> RunInterpretation:
    summary = delivery_result.summary
    assert summary is not None
    return {
        "run_id": str(run_id),
        "diagnostics": {
            "state": diagnostic_state,
            "reason": diagnostic_reason,
        },
        "terminal_state": "succeeded",
        "exit_code": exit_code,
        "result_kind": summary.result_kind.value,
        "interruption_signal": None,
        "primary_error": None,
        "associated_errors": [],
        "recovery_incomplete": recovery_incomplete,
        "delivery_manifest": _project_delivery_manifest_state(
            delivery_result
        ),
        "delivery": _project_delivery_summary(summary),
    }


def _unknown_run_interpretation(
    run_id: RunId,
    *,
    diagnostic_state: str,
    diagnostic_reason: str,
    delivery_result: DeliveryManifestReadResult | None = None,
    delivery_state: str | None = None,
    delivery_reason: str | None = None,
) -> RunInterpretation:
    if delivery_result is not None:
        delivery_manifest = _project_delivery_manifest_state(
            delivery_result
        )
    else:
        assert delivery_state is not None
        assert delivery_reason is not None
        delivery_manifest = {
            "state": delivery_state,
            "reason": delivery_reason,
        }
    return {
        "run_id": str(run_id),
        "diagnostics": {
            "state": diagnostic_state,
            "reason": diagnostic_reason,
        },
        "terminal_state": None,
        "exit_code": None,
        "result_kind": None,
        "interruption_signal": None,
        "primary_error": None,
        "associated_errors": [],
        "recovery_incomplete": None,
        "delivery_manifest": delivery_manifest,
        "delivery": None,
    }


def _project_delivery_manifest_state(
    delivery_result: DeliveryManifestReadResult,
) -> dict[str, str]:
    return {
        "state": delivery_result.state.value,
        "reason": delivery_result.reason.value,
    }


def _project_delivery_summary(
    summary: DeliveryManifestSummary,
) -> dict[str, Any]:
    return {
        "manifest_path": "delivery/manifest.json",
        "schema_version": DELIVERY_MANIFEST_SCHEMA_VERSION,
        "run_id": str(summary.run_id),
        "result_kind": summary.result_kind.value,
        "application_version": summary.application_version,
        "started_at": summary.started_at,
        "published_at": summary.published_at,
        "source": {
            "sha256": summary.source_sha256,
            "byte_length": summary.source_byte_length,
            "duration_ms": summary.source_duration_ms,
        },
        "short_video_count": summary.short_video_count,
        "file_count": summary.file_count,
    }


__all__ = ["RunInterpretation", "interpret_run"]
