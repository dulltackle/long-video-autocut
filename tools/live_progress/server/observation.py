"""把受管运行诊断投影为 ``live_progress.v1`` 语义快照。"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_auto_editor.diagnostics import (
    DiagnosticPackageReader,
    DiagnosticPackageReadState,
    DiagnosticPackageSnapshot,
)


SCHEMA_VERSION = "live_progress.v1"
RUN_ID_PATTERN = re.compile(
    r"run_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
STAGES = (
    "initialized",
    "preflight",
    "source_analysis",
    "transcription",
    "candidate_planning",
    "topic_review",
    "delivery_build",
    "delivery_verification",
    "publishing",
)
_EVENT_LIMIT = 64 * 1024 * 1024
_MANIFEST_LIMIT = 16 * 1024 * 1024
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _available(value: object) -> dict[str, object]:
    return {"status": "available", "value": value}


def _missing(entered: bool) -> dict[str, str]:
    return {"status": "not_observed" if entered else "not_applicable"}


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    body: bytes | None
    problem: str | None
    too_large: bool = False


@dataclass(frozen=True, slots=True)
class _TrustedDiagnostics:
    state: str
    reason: str
    events: tuple[dict[str, Any], ...]
    manifest_complete: bool


def _read_regular_file(
    directory_descriptor: int,
    name: str,
    *,
    maximum_bytes: int,
    symlink_reason: str,
    unreadable_reason: str,
) -> _FileSnapshot:
    try:
        before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return _FileSnapshot(None, None)
    except OSError:
        return _FileSnapshot(None, unreadable_reason)
    if stat.S_ISLNK(before.st_mode):
        return _FileSnapshot(None, symlink_reason)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (hasattr(os, "getuid") and before.st_uid != os.getuid())
    ):
        return _FileSnapshot(None, unreadable_reason)

    descriptor = -1
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_descriptor)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
        ):
            return _FileSnapshot(None, unreadable_reason)
        too_large = opened.st_size > maximum_bytes
        remaining = min(opened.st_size, maximum_bytes)
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                return _FileSnapshot(None, unreadable_reason)
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            return _FileSnapshot(None, unreadable_reason)
        return _FileSnapshot(b"".join(chunks), None, too_large)
    except OSError:
        return _FileSnapshot(None, unreadable_reason)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _owned_directory(descriptor: int) -> bool:
    status = os.fstat(descriptor)
    return stat.S_ISDIR(status.st_mode) and (
        not hasattr(os, "getuid") or status.st_uid == os.getuid()
    )


def _open_runs_directory(workspace_root: Path) -> int:
    descriptors: list[int] = []
    try:
        current = os.open(workspace_root, _DIRECTORY_FLAGS)
        descriptors.append(current)
        if not _owned_directory(current):
            raise OSError("workspace directory ownership changed")
        for name in ("work", "runs"):
            current = os.open(name, _DIRECTORY_FLAGS, dir_fd=current)
            descriptors.append(current)
            if not _owned_directory(current):
                raise OSError("workspace directory ownership changed")
        descriptors.pop()
        return current
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _trusted_events(body: bytes, count: int) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    for line in body.splitlines()[:count]:
        value = json.loads(line)
        if not isinstance(value, dict):
            break
        values.append(value)
    return tuple(values)


def _trusted_timeline_prefix(
    events: tuple[dict[str, Any], ...]
) -> tuple[tuple[dict[str, Any], ...], bool]:
    """在追加中的诊断包上执行阶段级时序约束。"""

    if not events:
        return events, False
    active_stage: tuple[str, str] | None = None
    completed_stages: set[str] = set()
    for index, event in enumerate(events):
        code = event["event_code"]
        if code == "run.initialized":
            if index != 0 or event["stage"] != "initialized":
                return events[:index], True
            continue
        if index == 0:
            return (), True
        if code == "run.completed":
            if index != len(events) - 1 or active_stage is not None:
                return events[:index], True
            continue
        if code == "stage.started":
            if active_stage is not None or event["stage"] in completed_stages:
                return events[:index], True
            active_stage = (event["stage"], event["module"])
            continue
        if code == "stage.completed":
            if active_stage != (event["stage"], event["module"]):
                return events[:index], True
            completed_stages.add(event["stage"])
            active_stage = None
            continue
        if active_stage is None or event["stage"] != active_stage[0]:
            return events[:index], True
    return events, False


def _read_diagnostics(workspace_root: Path, run_id: str) -> _TrustedDiagnostics:
    runs_descriptor = _open_runs_directory(workspace_root)
    run_descriptor = -1
    try:
        run_descriptor = os.open(run_id, _DIRECTORY_FLAGS, dir_fd=runs_descriptor)
        if not _owned_directory(run_descriptor):
            raise OSError("run directory ownership changed")
    except OSError:
        os.close(runs_descriptor)
        return _TrustedDiagnostics(
            "unavailable", "run_directory_unsafe", (), False
        )

    os.close(runs_descriptor)
    try:
        events_file = _read_regular_file(
            run_descriptor,
            "events.jsonl",
            maximum_bytes=_EVENT_LIMIT,
            symlink_reason="event_log_symlink",
            unreadable_reason="event_log_unreadable",
        )
        manifest_file = _read_regular_file(
            run_descriptor,
            "run.json",
            maximum_bytes=_MANIFEST_LIMIT,
            symlink_reason="run_manifest_symlink",
            unreadable_reason="run_manifest_unreadable",
        )
    finally:
        os.close(run_descriptor)
    if events_file.problem is not None:
        return _TrustedDiagnostics("unavailable", events_file.problem, (), False)
    event_bytes = events_file.body or b""
    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=event_bytes,
            manifest=manifest_file.body,
        )
    )
    events = _trusted_events(event_bytes, result.event_count)
    events, timeline_invalid = _trusted_timeline_prefix(events)

    if timeline_invalid:
        return _TrustedDiagnostics(
            "corrupt", "event_schema_invalid", events, False
        )

    if result.run_id is not None and str(result.run_id) != run_id:
        return _TrustedDiagnostics(
            "corrupt", "run_id_directory_mismatch", (), False
        )
    if events_file.too_large:
        return _TrustedDiagnostics("unavailable", "too_large", events, False)
    if manifest_file.problem is not None:
        return _TrustedDiagnostics(
            "unavailable", manifest_file.problem, events, False
        )
    if manifest_file.too_large:
        return _TrustedDiagnostics(
            "unavailable", "run_manifest_unreadable", events, False
        )

    state = result.state.value
    reason = result.reason.value
    if reason == "event_tail_truncated":
        reason = "event_tail_incomplete"
    elif reason == "manifest_missing":
        reason = "run_manifest_missing"
    return _TrustedDiagnostics(
        state,
        reason,
        events,
        result.state is DiagnosticPackageReadState.COMPLETE,
    )


def _event_with_code(
    events: tuple[dict[str, Any], ...], code: str
) -> dict[str, Any] | None:
    return next((event for event in events if event["event_code"] == code), None)


def _events_with_code(
    events: tuple[dict[str, Any], ...], code: str
) -> tuple[dict[str, Any], ...]:
    return tuple(event for event in events if event["event_code"] == code)


def _stage_lifecycle(
    stage: str, events: tuple[dict[str, Any], ...]
) -> dict[str, object]:
    started = next(
        (
            event
            for event in events
            if event["event_code"] == "stage.started" and event["stage"] == stage
        ),
        None,
    )
    completed = next(
        (
            event
            for event in events
            if event["event_code"] == "stage.completed" and event["stage"] == stage
        ),
        None,
    )
    if started is not None and completed is not None:
        attributes = completed["attributes"]
        return {
            "status": attributes["outcome"],
            "started_at": started["timestamp"],
            "ended_at": completed["timestamp"],
            "duration_ms": attributes["duration_ms"],
        }
    if started is not None:
        return {"status": "in_progress", "started_at": started["timestamp"]}
    if stage == "initialized":
        initialized = _event_with_code(events, "run.initialized")
        if initialized is not None:
            return {
                "status": "succeeded",
                "observed_at": initialized["timestamp"],
            }
    return {"status": "not_started"}


def _stage_entered(lifecycle: dict[str, object]) -> bool:
    return lifecycle["status"] != "not_started"


def _provider_selections(
    events: tuple[dict[str, Any], ...], entered: bool
) -> dict[str, object]:
    selected = {
        event["attributes"]["capability"]: event["attributes"]
        for event in _events_with_code(events, "external_service.selected")
    }
    result: dict[str, object] = {}
    for capability in (
        "transcription",
        "topic_review",
        "subtitle_optimization",
    ):
        attributes = selected.get(capability)
        result[capability] = (
            _missing(entered)
            if attributes is None
            else _available(
                {
                    "adapter_id": attributes["adapter_id"],
                    "provider_id": attributes["provider_id"],
                    "model_id": attributes["model_id"],
                }
            )
        )
    return result


def _initialized_stage(
    lifecycle: dict[str, object], events: tuple[dict[str, Any], ...]
) -> dict[str, object]:
    initialized = _event_with_code(events, "run.initialized")
    return {
        "stage": "initialized",
        "lifecycle": lifecycle,
        "result": {
            "application_version": (
                _missing(False)
                if initialized is None
                else _available(initialized["attributes"]["application_version"])
            )
        },
        "progress": {},
        "error_ids": [],
    }


def _preflight_stage(
    lifecycle: dict[str, object], events: tuple[dict[str, Any], ...]
) -> dict[str, object]:
    entered = _stage_entered(lifecycle)
    environment = _event_with_code(events, "environment.observed")
    configuration = _event_with_code(events, "configuration.observed")
    if environment is None:
        certified_platform = _missing(entered)
        versions = {name: _missing(entered) for name in ("python", "ffmpeg", "ffprobe")}
        font = _missing(entered)
        outcome = _missing(entered)
    else:
        attributes = environment["attributes"]
        certified_platform = _available(attributes["certified_platform"])
        versions = {
            "python": _available(attributes["python_version"]),
            "ffmpeg": _available(attributes["ffmpeg_version"]),
            "ffprobe": _available(attributes["ffprobe_version"]),
        }
        font = _available(attributes["font"])
        outcome = _available(attributes["preflight_outcome"])
    return {
        "stage": "preflight",
        "lifecycle": lifecycle,
        "result": {
            "certified_platform": certified_platform,
            "tool_versions": versions,
            "font": font,
            "provider_selections": _provider_selections(events, entered),
        },
        "progress": {
            "configuration_observed": (
                _missing(entered) if configuration is None else _available(True)
            ),
            "environment_outcome": outcome,
        },
        "error_ids": [],
    }


def _source_stage(
    lifecycle: dict[str, object], events: tuple[dict[str, Any], ...]
) -> dict[str, object]:
    entered = _stage_entered(lifecycle)
    observed = _event_with_code(events, "source.observed")
    fields = ("byte_length", "duration_ms", "course_context_provided")
    result = {
        field: (
            _missing(entered)
            if observed is None
            else _available(observed["attributes"][field])
        )
        for field in fields
    }
    return {
        "stage": "source_analysis",
        "lifecycle": lifecycle,
        "result": result,
        "progress": {},
        "error_ids": [],
    }


def _later_stage(
    stage: str, lifecycle: dict[str, object]
) -> dict[str, object]:
    entered = _stage_entered(lifecycle)
    missing = lambda: _missing(entered)
    results: dict[str, dict[str, object]] = {
        "transcription": {
            "transcript_chunk_count": missing(),
            "coverage_recovery_count": missing(),
            "transcript_cache": missing(),
        },
        "candidate_planning": {"candidate_count": missing()},
        "topic_review": {
            "reviewed_candidate_count": missing(),
            "cache_observation": missing(),
        },
        "delivery_build": {
            "short_video_count": missing(),
            "created_artifacts_by_role": missing(),
            "subtitle_cache_observation": missing(),
        },
        "delivery_verification": {
            "verified_item_count": missing(),
            "verified_short_video_count": missing(),
            "verified_items_by_role": missing(),
        },
        "publishing": {"publication_fact": missing()},
    }
    request_progress = {
        "completed_request_count": missing(),
        "inflight_request_count": missing(),
        "transport_retry_count": missing(),
        "latest_retry": missing(),
    }
    progress: dict[str, dict[str, object]] = {
        "transcription": request_progress,
        "candidate_planning": {},
        "topic_review": request_progress.copy(),
        "delivery_build": {**request_progress, "delivery_state": missing()},
        "delivery_verification": {"delivery_state": missing()},
        "publishing": {"delivery_state": missing()},
    }
    return {
        "stage": stage,
        "lifecycle": lifecycle,
        "result": results[stage],
        "progress": progress[stage],
        "error_ids": [],
    }


def _terminal(
    diagnostics: _TrustedDiagnostics,
) -> tuple[dict[str, object], dict[str, object] | None]:
    terminal = next(
        (
            event
            for event in reversed(diagnostics.events)
            if event["event_code"] == "run.completed"
        ),
        None,
    )
    if terminal is None:
        return {"status": "not_observed"}, None
    attributes = terminal["attributes"]
    outcome = attributes["outcome"]
    interruption = next(
        (
            event
            for event in diagnostics.events
            if event["event_code"] == "interruption.requested"
        ),
        None,
    )
    facts = {
        "evidence": (
            "run_manifest" if diagnostics.manifest_complete else "terminal_event"
        ),
        "outcome": outcome,
        "ended_at": _available(terminal["timestamp"]),
        "duration_ms": _available(attributes["duration_ms"]),
        "exit_code": _available(attributes["exit_code"]),
        "result_kind": (
            attributes["result_kind"]
            if outcome == "succeeded"
            else {"status": "not_applicable"}
        ),
        "interruption_signal": (
            _available(interruption["attributes"]["signal"])
            if outcome == "interrupted" and interruption is not None
            else {"status": "not_applicable"}
        ),
    }
    return _available(facts), facts


def project_run(workspace_root: Path, run_id: str) -> dict[str, object]:
    """读取一个已发现运行并返回不含物理路径的语义快照。"""

    diagnostics = _read_diagnostics(workspace_root, run_id)
    events = diagnostics.events
    terminal, terminal_facts = _terminal(diagnostics)
    observation_state = (
        terminal_facts["outcome"]
        if terminal_facts is not None
        else (
            "record_corrupt"
            if diagnostics.state in {"corrupt", "unavailable"}
            else "unclosed"
        )
    )
    started = _event_with_code(events, "run.initialized")
    last = events[-1] if events else None
    stage_lifecycles = {
        stage: _stage_lifecycle(stage, events) for stage in STAGES
    }
    stages = [
        _initialized_stage(stage_lifecycles["initialized"], events),
        _preflight_stage(stage_lifecycles["preflight"], events),
        _source_stage(stage_lifecycles["source_analysis"], events),
        *[
            _later_stage(stage, stage_lifecycles[stage])
            for stage in STAGES[3:]
        ],
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "observation": {
            "state": observation_state,
            "stage": (
                {"status": "not_observed"}
                if last is None
                else _available(last["stage"])
            ),
            "started_at": (
                {"status": "not_observed"}
                if started is None
                else _available(started["timestamp"])
            ),
            "ended_at": (
                {"status": "not_observed"}
                if terminal_facts is None
                else terminal_facts["ended_at"]
            ),
            "duration_ms": (
                {"status": "not_observed"}
                if terminal_facts is None
                else terminal_facts["duration_ms"]
            ),
            "last_event_at": (
                {"status": "not_observed"}
                if last is None
                else _available(last["timestamp"])
            ),
        },
        "diagnostics": {
            "state": diagnostics.state,
            "reason": diagnostics.reason,
            "verified_event_count": len(events),
        },
        "terminal": terminal,
        "stages": stages,
        "errors": {
            "by_id": {},
            "primary_error_id": {"status": "not_observed"},
            "associated_error_ids": {"status": "not_observed"},
            "recovery_incomplete": {"status": "not_observed"},
        },
        "delivery": {
            "status": "not_observed",
            "short_video_count": {"status": "not_observed"},
            "preview": {"status": "not_ready"},
        },
    }


def discover_runs(workspace_root: Path) -> tuple[str, ...]:
    """只返回名称符合封闭运行标识的已发现目录项。"""

    descriptor = _open_runs_directory(workspace_root)
    try:
        names = tuple(os.listdir(descriptor))
    finally:
        os.close(descriptor)
    return tuple(sorted(name for name in names if RUN_ID_PATTERN.fullmatch(name)))
