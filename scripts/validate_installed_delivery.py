#!/usr/bin/env python3
"""不依赖候选应用实现的标准交付物消费者校验器。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from pathlib import Path, PurePosixPath
from typing import Any


_RESULT_SCHEMA = "independent_delivery_validation.v1"
_EXPECTED_TRANSCRIPT_SCHEMA = "installed_acceptance_transcript.v1"
_MANIFEST_FIELDS = {
    "application_version",
    "documents",
    "execution",
    "files",
    "published_at",
    "result_kind",
    "run_id",
    "schema_version",
    "source",
    "started_at",
    "terminal_state",
}
_TRANSCRIPT_FIELDS = {
    "chunks",
    "run_id",
    "schema_version",
    "source_duration_ms",
    "speech_presence",
    "transcript_id",
}
_TRANSCRIPT_CHUNK_REQUIRED_FIELDS = {
    "end_ms",
    "start_ms",
    "text",
    "transcript_chunk_id",
}
_PLAN_FIELDS = {
    "candidate_count",
    "candidates",
    "plan_id",
    "published_count",
    "result_kind",
    "run_id",
    "schema_version",
    "transcript_id",
}
_CANDIDATE_FIELDS = {
    "boundary_remedy",
    "candidate_id",
    "final_range",
    "initial_range",
    "review",
    "selection",
    "transcript_chunk_ids",
}
_REVIEW_FIELDS = {
    "boundary_fix_end_ms",
    "boundary_fix_start_ms",
    "boundary_fix_suggestion",
    "export_decision",
    "keywords",
    "learning_value",
    "needs_human_review",
    "publish_ready_score",
    "reject_reason",
    "share_value",
    "summary",
    "title",
    "topic_complete",
    "topic_name",
}
_METADATA_FIELDS = {
    "result_kind",
    "run_id",
    "schema_version",
    "series",
    "short_videos",
}
_SHORT_VIDEO_FIELDS = {
    "duration_ms",
    "end_ms",
    "keywords",
    "media",
    "short_video_id",
    "source_candidate_id",
    "start_ms",
    "subtitles",
    "summary",
    "title",
    "topic_name",
}
_FILE_FIELDS = {"byte_length", "media_type", "path", "role", "sha256"}
_FIXED_ARTIFACTS = {
    "metadata.json": ("short_video_catalog", "application/json"),
    "plan.json": ("clip_plan", "application/json"),
    "report.md": ("human_report", "text/markdown"),
    "transcript.json": ("faithful_transcript", "application/json"),
    "transcript.srt": (
        "faithful_transcript_rendering",
        "application/x-subrip",
    ),
}
_DOCUMENT_PATHS = {
    "metadata": "metadata.json",
    "plan": "plan.json",
    "report": "report.md",
    "transcript": "transcript.json",
    "transcript_rendering": "transcript.srt",
}
_ID_PATTERNS = {
    "candidate": re.compile(r"candidate_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"),
    "plan": re.compile(r"plan_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"),
    "run": re.compile(r"run_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"),
    "series": re.compile(r"series_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"),
    "short_video": re.compile(r"short_video_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"),
    "transcript": re.compile(r"transcript_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"),
    "transcript_chunk": re.compile(r"transcript_chunk_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"),
}
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?")
_RFC3339_UTC = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?Z"
)
_REJECTION_REASONS = {
    "boundary_remedy_invalid",
    "excluded_content",
    "max_clips_limit",
    "needs_human_review",
    "publish_ready_score_below_threshold",
    "review_rejected",
    "topic_incomplete",
}
_MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
_MAX_DELIVERY_FILES = 4096
_FILE_READ_BYTES = 1024 * 1024
_PROBE_TIMEOUT_SECONDS = 30
_MAX_PROBE_OUTPUT_BYTES = 1024 * 1024
_OPEN_READ_FLAGS = (
    os.O_RDONLY
    | os.O_CLOEXEC
    | os.O_NONBLOCK
    | getattr(os, "O_NOFOLLOW", 0)
)
_OPEN_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | os.O_CLOEXEC
    | os.O_DIRECTORY
    | getattr(os, "O_NOFOLLOW", 0)
)


@dataclass(frozen=True, slots=True)
class _ResultTarget:
    parent: Path
    name: str
    parent_fact: tuple[int, ...]


@dataclass(slots=True)
class _DeliveryTree:
    path: Path
    root_fd: int
    clips_fd: int
    root_fact: tuple[int, ...]
    clips_fact: tuple[int, ...]
    file_facts: dict[str, tuple[int, ...]]
    root_files: frozenset[str]
    clip_files: frozenset[str]

    def close(self) -> None:
        for descriptor in (self.clips_fd, self.root_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class _ArtifactSnapshot:
    documents: dict[str, bytes]
    media_entries: dict[str, Mapping[str, Any]]
    artifact_count: int


@dataclass(slots=True)
class _ExternalFile:
    path: Path
    descriptor: int
    fact: tuple[int, ...]

    def close(self) -> None:
        try:
            os.close(self.descriptor)
        except OSError:
            pass


class _DuplicateField(ValueError):
    pass


class ValidationFailure(RuntimeError):
    """只携带稳定原因码的独立校验失败。"""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateField(key)
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(value)


def _load_json_bytes(contents: bytes, *, reason: str) -> Any:
    try:
        if len(contents) > _MAX_DOCUMENT_BYTES:
            raise ValueError
        return json.loads(
            contents,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise ValidationFailure(reason) from None


def _stat_fact(status: os.stat_result) -> tuple[int, ...]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_nlink,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _regular_file_fact(status: os.stat_result, reason: str) -> tuple[int, ...]:
    if not stat.S_ISREG(status.st_mode):
        raise ValidationFailure(reason)
    return _stat_fact(status)


def _directory_fact(status: os.stat_result, reason: str) -> tuple[int, ...]:
    if not stat.S_ISDIR(status.st_mode):
        raise ValidationFailure(reason)
    return _stat_fact(status)


def _scan_directory(
    descriptor: int,
    *,
    allow_directories: bool,
    reason: str,
    entry_limit: int,
) -> tuple[dict[str, tuple[int, ...]], dict[str, tuple[int, ...]]]:
    files: dict[str, tuple[int, ...]] = {}
    directories: dict[str, tuple[int, ...]] = {}
    try:
        with os.scandir(descriptor) as entries:
            for index, entry in enumerate(entries, start=1):
                if index > entry_limit:
                    raise ValidationFailure("artifact.file_set_mismatch")
                name = entry.name
                if not isinstance(name, str):
                    raise ValidationFailure(reason)
                status = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(status.st_mode):
                    raise ValidationFailure("path.invalid")
                if stat.S_ISREG(status.st_mode):
                    files[name] = _stat_fact(status)
                    continue
                if allow_directories and stat.S_ISDIR(status.st_mode):
                    directories[name] = _stat_fact(status)
                    continue
                raise ValidationFailure("path.invalid")
    except ValidationFailure:
        raise
    except (OSError, UnicodeError):
        raise ValidationFailure(reason) from None
    return files, directories


def _open_delivery_tree(path: Path) -> _DeliveryTree:
    root_fd = -1
    clips_fd = -1
    try:
        root_fd = os.open(path, _OPEN_DIRECTORY_FLAGS)
        root_fact = _directory_fact(os.fstat(root_fd), "delivery.invalid")
        root_files, root_directories = _scan_directory(
            root_fd,
            allow_directories=True,
            reason="artifact.unreadable",
            entry_limit=len(_FIXED_ARTIFACTS) + 2,
        )
        expected_root_files = {"manifest.json", *_FIXED_ARTIFACTS}
        if set(root_files) != expected_root_files or set(root_directories) != {"clips"}:
            raise ValidationFailure("artifact.file_set_mismatch")
        clips_fd = os.open("clips", _OPEN_DIRECTORY_FLAGS, dir_fd=root_fd)
        clips_fact = _directory_fact(os.fstat(clips_fd), "path.invalid")
        if clips_fact != root_directories["clips"]:
            raise ValidationFailure("artifact.changed")
        clip_files, clip_directories = _scan_directory(
            clips_fd,
            allow_directories=False,
            reason="artifact.unreadable",
            entry_limit=_MAX_DELIVERY_FILES - len(root_files),
        )
        if clip_directories:
            raise ValidationFailure("artifact.file_set_mismatch")
        file_facts = dict(root_files)
        file_facts.update(
            {f"clips/{name}": fact for name, fact in clip_files.items()}
        )
        return _DeliveryTree(
            path=path,
            root_fd=root_fd,
            clips_fd=clips_fd,
            root_fact=root_fact,
            clips_fact=clips_fact,
            file_facts=file_facts,
            root_files=frozenset(root_files),
            clip_files=frozenset(clip_files),
        )
    except ValidationFailure:
        if clips_fd >= 0:
            os.close(clips_fd)
        if root_fd >= 0:
            os.close(root_fd)
        raise
    except OSError:
        if clips_fd >= 0:
            os.close(clips_fd)
        if root_fd >= 0:
            os.close(root_fd)
        raise ValidationFailure("delivery.invalid") from None


def _open_tree_file(tree: _DeliveryTree, path: str, reason: str) -> int:
    parsed = PurePosixPath(path)
    if len(parsed.parts) == 1:
        parent_fd = tree.root_fd
        name = parsed.name
    elif len(parsed.parts) == 2 and parsed.parts[0] == "clips":
        parent_fd = tree.clips_fd
        name = parsed.parts[1]
    else:
        raise ValidationFailure("path.invalid")
    expected = tree.file_facts.get(path)
    if expected is None:
        raise ValidationFailure("artifact.file_set_mismatch")
    descriptor = -1
    try:
        descriptor = os.open(name, _OPEN_READ_FLAGS, dir_fd=parent_fd)
        observed = _regular_file_fact(os.fstat(descriptor), reason)
        if observed != expected:
            raise ValidationFailure("artifact.changed")
        return descriptor
    except ValidationFailure:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        raise ValidationFailure(reason) from None


def _assert_unchanged(
    descriptor: int,
    expected: tuple[int, ...],
    reason: str = "artifact.changed",
) -> None:
    try:
        observed = _regular_file_fact(os.fstat(descriptor), reason)
    except OSError:
        raise ValidationFailure(reason) from None
    if observed != expected:
        raise ValidationFailure(reason)


def _read_tree_document(tree: _DeliveryTree, path: str, reason: str) -> bytes:
    descriptor = _open_tree_file(tree, path, reason)
    expected = tree.file_facts[path]
    try:
        if expected[4] > _MAX_DOCUMENT_BYTES:
            raise ValidationFailure(reason)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_FILE_READ_BYTES, _MAX_DOCUMENT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_DOCUMENT_BYTES:
                raise ValidationFailure(reason)
        _assert_unchanged(descriptor, expected)
        return b"".join(chunks)
    except ValidationFailure:
        raise
    except OSError:
        raise ValidationFailure(reason) from None
    finally:
        os.close(descriptor)


def _open_external_file(path: Path, reason: str) -> _ExternalFile:
    descriptor = -1
    try:
        descriptor = os.open(path, _OPEN_READ_FLAGS)
        fact = _regular_file_fact(os.fstat(descriptor), reason)
        return _ExternalFile(path=path, descriptor=descriptor, fact=fact)
    except ValidationFailure:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        raise ValidationFailure(reason) from None


def _read_external_document(path: Path, reason: str) -> bytes:
    external = _open_external_file(path, reason)
    try:
        if external.fact[4] > _MAX_DOCUMENT_BYTES:
            raise ValidationFailure(reason)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                external.descriptor,
                min(_FILE_READ_BYTES, _MAX_DOCUMENT_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_DOCUMENT_BYTES:
                raise ValidationFailure(reason)
        _assert_unchanged(external.descriptor, external.fact, reason)
        return b"".join(chunks)
    except OSError:
        raise ValidationFailure(reason) from None
    finally:
        external.close()


def _object(value: Any, fields: set[str], reason: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationFailure(reason)
    return value


def _array(value: Any, reason: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationFailure(reason)
    return value


def _string(value: Any, reason: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValidationFailure(reason)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ValidationFailure(reason) from None
    return value


def _integer(value: Any, reason: str, *, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationFailure(reason)
    if value < (1 if positive else 0):
        raise ValidationFailure(reason)
    return value


def _nullable_integer(value: Any, reason: str) -> int | None:
    if value is None:
        return None
    return _integer(value, reason)


def _boolean(value: Any, reason: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationFailure(reason)
    return value


def _identifier(value: Any, kind: str, reason: str) -> str:
    text = _string(value, reason)
    if _ID_PATTERNS[kind].fullmatch(text) is None:
        raise ValidationFailure(reason)
    return text


def _safe_path(value: Any) -> str:
    text = _string(value, "path.invalid", nonempty=True)
    encoded = text.encode("utf-8")
    if (
        len(encoded) > 4096
        or text != unicodedata.normalize("NFC", text)
        or "\\" in text
        or "\x00" in text
        or not text.isprintable()
    ):
        raise ValidationFailure("path.invalid")
    parsed = PurePosixPath(text)
    if (
        parsed.is_absolute()
        or parsed.as_posix() != text
        or any(
            part in {"", ".", ".."}
            or len(part.encode("utf-8")) > 255
            or not part.isprintable()
            for part in parsed.parts
        )
    ):
        raise ValidationFailure("path.invalid")
    return text


def _utc_timestamp(value: Any, reason: str) -> int:
    text = _string(value, reason)
    if _RFC3339_UTC.fullmatch(text) is None:
        raise ValidationFailure(reason)
    without_zone = text.removesuffix("Z")
    if "." in without_zone:
        seconds_text, fraction = without_zone.rsplit(".", 1)
    else:
        seconds_text, fraction = without_zone, ""
    try:
        parsed = datetime.fromisoformat(seconds_text + "+00:00")
    except ValueError:
        raise ValidationFailure(reason) from None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValidationFailure(reason)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed - epoch
    nanoseconds = int(fraction.ljust(9, "0")) if fraction else 0
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + nanoseconds


def _hash_descriptor(
    descriptor: int,
    expected: tuple[int, ...],
    reason: str,
    *,
    changed_reason: str = "artifact.changed",
) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_length = 0
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, _FILE_READ_BYTES)
            if not chunk:
                break
            byte_length += len(chunk)
            digest.update(chunk)
        _assert_unchanged(descriptor, expected, changed_reason)
        return byte_length, "sha256:" + digest.hexdigest()
    except OSError:
        raise ValidationFailure(reason) from None


def _revalidate_external(external: _ExternalFile, reason: str) -> None:
    _assert_unchanged(external.descriptor, external.fact, reason)
    try:
        path_fact = _regular_file_fact(
            os.stat(external.path, follow_symlinks=False),
            reason,
        )
    except OSError:
        raise ValidationFailure(reason) from None
    if path_fact != external.fact:
        raise ValidationFailure(reason)


def _revalidate_delivery_tree(tree: _DeliveryTree) -> None:
    try:
        if _directory_fact(os.fstat(tree.root_fd), "artifact.changed") != tree.root_fact:
            raise ValidationFailure("artifact.changed")
        if _directory_fact(os.fstat(tree.clips_fd), "artifact.changed") != tree.clips_fact:
            raise ValidationFailure("artifact.changed")
        if (
            _directory_fact(
                os.stat(tree.path, follow_symlinks=False),
                "artifact.changed",
            )
            != tree.root_fact
        ):
            raise ValidationFailure("artifact.changed")
        root_files, root_directories = _scan_directory(
            tree.root_fd,
            allow_directories=True,
            reason="artifact.changed",
            entry_limit=len(_FIXED_ARTIFACTS) + 2,
        )
        if (
            frozenset(root_files) != tree.root_files
            or set(root_directories) != {"clips"}
            or root_directories["clips"] != tree.clips_fact
        ):
            raise ValidationFailure("artifact.changed")
        clip_files, clip_directories = _scan_directory(
            tree.clips_fd,
            allow_directories=False,
            reason="artifact.changed",
            entry_limit=_MAX_DELIVERY_FILES - len(root_files),
        )
        if clip_directories or frozenset(clip_files) != tree.clip_files:
            raise ValidationFailure("artifact.changed")
        current_facts = dict(root_files)
        current_facts.update(
            {f"clips/{name}": fact for name, fact in clip_files.items()}
        )
        if current_facts != tree.file_facts:
            raise ValidationFailure("artifact.changed")
    except ValidationFailure:
        raise
    except OSError:
        raise ValidationFailure("artifact.changed") from None


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    value = _object(manifest, _MANIFEST_FIELDS, "manifest.schema_invalid")
    if value["schema_version"] != "delivery_manifest.v1":
        raise ValidationFailure("manifest.schema_invalid")
    _identifier(value["run_id"], "run", "manifest.schema_invalid")
    if value["terminal_state"] != "succeeded":
        raise ValidationFailure("manifest.schema_invalid")
    if value["result_kind"] not in {"clips", "empty"}:
        raise ValidationFailure("manifest.schema_invalid")
    if _VERSION.fullmatch(_string(value["application_version"], "manifest.schema_invalid")) is None:
        raise ValidationFailure("manifest.schema_invalid")
    started_at = _utc_timestamp(value["started_at"], "manifest.schema_invalid")
    published_at = _utc_timestamp(value["published_at"], "manifest.schema_invalid")
    if published_at < started_at:
        raise ValidationFailure("manifest.schema_invalid")
    source = _object(
        value["source"],
        {"byte_length", "duration_ms", "sha256"},
        "manifest.schema_invalid",
    )
    _integer(source["byte_length"], "manifest.schema_invalid", positive=True)
    _integer(source["duration_ms"], "manifest.schema_invalid", positive=True)
    if _SHA256.fullmatch(_string(source["sha256"], "manifest.schema_invalid")) is None:
        raise ValidationFailure("manifest.schema_invalid")
    documents = _object(
        value["documents"],
        set(_DOCUMENT_PATHS),
        "manifest.schema_invalid",
    )
    for name, expected_path in _DOCUMENT_PATHS.items():
        fields = {"path"}
        if name in {"transcript", "transcript_rendering"}:
            fields.add("transcript_id")
        elif name == "plan":
            fields.add("plan_id")
        document = _object(documents[name], fields, "manifest.schema_invalid")
        if _safe_path(document["path"]) != expected_path:
            raise ValidationFailure("manifest.schema_invalid")
    execution = _object(
        value["execution"],
        {"subtitle_optimization"},
        "manifest.schema_invalid",
    )
    counts = _object(
        execution["subtitle_optimization"],
        {
            "cache_hit_count",
            "cache_miss_count",
            "model_request_count",
            "semantic_retry_count",
            "short_video_count",
            "transport_attempt_count",
            "transport_retry_count",
            "window_count",
        },
        "manifest.schema_invalid",
    )
    for count in counts.values():
        _integer(count, "manifest.schema_invalid")
    if (
        counts["semantic_retry_count"] > counts["model_request_count"]
        or counts["transport_retry_count"] > counts["transport_attempt_count"]
        or counts["cache_hit_count"] + counts["cache_miss_count"]
        != counts["window_count"]
        or counts["model_request_count"] < counts["cache_miss_count"]
        or counts["window_count"] < counts["short_video_count"]
    ):
        raise ValidationFailure("manifest.schema_invalid")
    files = _array(value["files"], "manifest.schema_invalid")
    paths = [
        _safe_path(
            _object(entry, _FILE_FIELDS, "manifest.schema_invalid")["path"]
        )
        for entry in files
    ]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValidationFailure("artifact.file_set_mismatch")
    return value


def _validate_artifacts(
    tree: _DeliveryTree,
    manifest: Mapping[str, Any],
) -> _ArtifactSnapshot:
    file_entries: dict[str, Mapping[str, Any]] = {}
    for raw_entry in manifest["files"]:
        entry = _object(raw_entry, _FILE_FIELDS, "artifact.schema_invalid")
        path = _safe_path(entry["path"])
        if path in file_entries or path == "manifest.json":
            raise ValidationFailure("artifact.schema_invalid")
        byte_length = _integer(entry["byte_length"], "artifact.schema_invalid")
        digest = _string(entry["sha256"], "artifact.schema_invalid")
        if _SHA256.fullmatch(digest) is None:
            raise ValidationFailure("artifact.schema_invalid")
        role = _string(entry["role"], "artifact.schema_invalid")
        media_type = _string(entry["media_type"], "artifact.schema_invalid")
        fixed = _FIXED_ARTIFACTS.get(path)
        if fixed is not None and fixed != (role, media_type):
            raise ValidationFailure("artifact.schema_invalid")
        if path.startswith("clips/") and (
            len(PurePosixPath(path).parts) != 2
            or PurePosixPath(path).suffix != ".mp4"
            or role != "short_video_media"
            or media_type != "video/mp4"
        ):
            raise ValidationFailure("artifact.schema_invalid")
        if fixed is None and not path.startswith("clips/"):
            raise ValidationFailure("artifact.schema_invalid")
        file_entries[path] = entry

    if not set(_FIXED_ARTIFACTS).issubset(file_entries):
        raise ValidationFailure("artifact.file_set_mismatch")
    actual_files = set(tree.file_facts) - {"manifest.json"}
    if actual_files != set(file_entries):
        raise ValidationFailure("artifact.file_set_mismatch")
    documents: dict[str, bytes] = {}
    media_entries: dict[str, Mapping[str, Any]] = {}
    for path, entry in file_entries.items():
        if path.startswith("clips/"):
            media_entries[path] = entry
            continue
        contents = _read_tree_document(tree, path, "artifact.unreadable")
        if len(contents) != entry["byte_length"] or (
            "sha256:" + hashlib.sha256(contents).hexdigest() != entry["sha256"]
        ):
            raise ValidationFailure("artifact.digest_mismatch")
        documents[path] = contents
    return _ArtifactSnapshot(
        documents=documents,
        media_entries=media_entries,
        artifact_count=len(file_entries),
    )


def _validate_transcript(
    value: Any,
    *,
    run_id: str,
    source_duration_ms: int,
) -> tuple[dict[str, Any], set[str]]:
    transcript = _object(value, _TRANSCRIPT_FIELDS, "transcript.schema_invalid")
    if (
        transcript["schema_version"] != "transcript.v1"
        or transcript["run_id"] != run_id
        or transcript["source_duration_ms"] != source_duration_ms
        or transcript["speech_presence"] not in {"present", "absent"}
    ):
        raise ValidationFailure("transcript.schema_invalid")
    _identifier(transcript["transcript_id"], "transcript", "transcript.schema_invalid")
    chunks = _array(transcript["chunks"], "transcript.schema_invalid")
    if (transcript["speech_presence"] == "present") != bool(chunks):
        raise ValidationFailure("transcript.schema_invalid")
    identifiers: set[str] = set()
    previous_end = 0
    for raw_chunk in chunks:
        if not isinstance(raw_chunk, dict):
            raise ValidationFailure("transcript.schema_invalid")
        fields = set(raw_chunk)
        if fields != _TRANSCRIPT_CHUNK_REQUIRED_FIELDS and fields != (
            _TRANSCRIPT_CHUNK_REQUIRED_FIELDS | {"char_spans_ms"}
        ):
            raise ValidationFailure("transcript.schema_invalid")
        identifier = _identifier(
            raw_chunk["transcript_chunk_id"],
            "transcript_chunk",
            "transcript.schema_invalid",
        )
        if identifier in identifiers:
            raise ValidationFailure("transcript.schema_invalid")
        identifiers.add(identifier)
        start = _integer(raw_chunk["start_ms"], "transcript.schema_invalid")
        end = _integer(raw_chunk["end_ms"], "transcript.schema_invalid", positive=True)
        _string(raw_chunk["text"], "transcript.schema_invalid", nonempty=True)
        if not previous_end <= start < end <= source_duration_ms:
            raise ValidationFailure("transcript.schema_invalid")
        previous_end = end
        if "char_spans_ms" in raw_chunk:
            spans = _array(raw_chunk["char_spans_ms"], "transcript.schema_invalid")
            if len(spans) != len(raw_chunk["text"]):
                raise ValidationFailure("transcript.schema_invalid")
            previous_span_end = start
            for span in spans:
                item = _object(
                    span,
                    {"start_ms", "end_ms"},
                    "transcript.schema_invalid",
                )
                span_start = _integer(item["start_ms"], "transcript.schema_invalid")
                span_end = _integer(item["end_ms"], "transcript.schema_invalid", positive=True)
                if not previous_span_end <= span_start < span_end <= end:
                    raise ValidationFailure("transcript.schema_invalid")
                previous_span_end = span_end
    return transcript, identifiers


def _validate_plan(
    value: Any,
    *,
    run_id: str,
    transcript_id: str,
    transcript_chunks: set[str],
) -> tuple[dict[str, Any], set[str], set[str]]:
    plan = _object(value, _PLAN_FIELDS, "plan.schema_invalid")
    if (
        plan["schema_version"] != "clip_plan.v1"
        or plan["run_id"] != run_id
        or plan["transcript_id"] != transcript_id
        or plan["result_kind"] not in {"clips", "empty"}
    ):
        raise ValidationFailure("plan.schema_invalid")
    _identifier(plan["plan_id"], "plan", "plan.schema_invalid")
    candidates = _array(plan["candidates"], "plan.schema_invalid")
    if _integer(plan["candidate_count"], "plan.schema_invalid") != len(candidates):
        raise ValidationFailure("plan.schema_invalid")
    published_count = _integer(plan["published_count"], "plan.schema_invalid")
    candidate_ids: set[str] = set()
    published_ids: set[str] = set()
    for raw_candidate in candidates:
        candidate = _object(raw_candidate, _CANDIDATE_FIELDS, "plan.schema_invalid")
        candidate_id = _identifier(
            candidate["candidate_id"],
            "candidate",
            "plan.schema_invalid",
        )
        if candidate_id in candidate_ids:
            raise ValidationFailure("plan.reference_invalid")
        candidate_ids.add(candidate_id)
        raw_chunk_ids = _array(
            candidate["transcript_chunk_ids"],
            "plan.schema_invalid",
        )
        chunk_ids = [
            _identifier(item, "transcript_chunk", "plan.reference_invalid")
            for item in raw_chunk_ids
        ]
        if not chunk_ids or len(chunk_ids) != len(set(chunk_ids)) or any(
            item not in transcript_chunks for item in chunk_ids
        ):
            raise ValidationFailure("plan.reference_invalid")
        ranges: dict[str, tuple[int, int]] = {}
        for name in ("initial_range", "final_range"):
            material_range = _object(
                candidate[name], {"start_ms", "end_ms"}, "plan.schema_invalid"
            )
            start = _integer(material_range["start_ms"], "plan.schema_invalid")
            end = _integer(material_range["end_ms"], "plan.schema_invalid", positive=True)
            if start >= end:
                raise ValidationFailure("plan.schema_invalid")
            ranges[name] = (start, end)
        remedy = _object(
            candidate["boundary_remedy"],
            {"requested_end_ms", "requested_start_ms", "status", "suggestion"},
            "plan.schema_invalid",
        )
        if remedy["status"] not in {"not_needed", "applied", "invalid"}:
            raise ValidationFailure("plan.schema_invalid")
        suggestion = _string(remedy["suggestion"], "plan.schema_invalid")
        requested_start = _nullable_integer(
            remedy["requested_start_ms"],
            "plan.schema_invalid",
        )
        requested_end = _nullable_integer(
            remedy["requested_end_ms"],
            "plan.schema_invalid",
        )
        if (requested_start is None) != (requested_end is None) or (
            requested_start is not None
            and requested_end is not None
            and requested_start >= requested_end
        ):
            raise ValidationFailure("plan.schema_invalid")
        review = _object(candidate["review"], _REVIEW_FIELDS, "plan.schema_invalid")
        for name in ("topic_name", "title", "summary"):
            _string(review[name], "plan.schema_invalid", nonempty=True)
        for name in (
            "boundary_fix_suggestion",
            "reject_reason",
        ):
            _string(review[name], "plan.schema_invalid")
        for name in ("topic_complete", "needs_human_review"):
            _boolean(review[name], "plan.schema_invalid")
        for name, maximum in (
            ("learning_value", 10),
            ("share_value", 10),
            ("publish_ready_score", 100),
        ):
            if _integer(review[name], "plan.schema_invalid") > maximum:
                raise ValidationFailure("plan.schema_invalid")
        if review["export_decision"] not in {
            "needs_review",
            "publish_ready",
            "reject",
        }:
            raise ValidationFailure("plan.schema_invalid")
        keywords = _array(review["keywords"], "plan.schema_invalid")
        if any(
            not _string(keyword, "plan.schema_invalid").strip()
            for keyword in keywords
        ):
            raise ValidationFailure("plan.schema_invalid")
        fix_start = _nullable_integer(
            review["boundary_fix_start_ms"],
            "plan.schema_invalid",
        )
        fix_end = _nullable_integer(
            review["boundary_fix_end_ms"],
            "plan.schema_invalid",
        )
        if (fix_start is None) != (fix_end is None) or (
            fix_start is not None
            and fix_end is not None
            and fix_start >= fix_end
        ):
            raise ValidationFailure("plan.schema_invalid")
        selection = candidate["selection"]
        if not isinstance(selection, dict) or selection.get("outcome") not in {
            "published",
            "rejected",
        }:
            raise ValidationFailure("plan.schema_invalid")
        if selection["outcome"] == "published":
            selected = _object(
                selection, {"outcome", "short_video_id"}, "plan.schema_invalid"
            )
            short_video_id = _identifier(
                selected["short_video_id"], "short_video", "plan.schema_invalid"
            )
            if short_video_id in published_ids:
                raise ValidationFailure("plan.reference_invalid")
            published_ids.add(short_video_id)
        else:
            rejected = _object(
                selection,
                {"human_review_reason", "needs_human_review", "outcome", "reason_code"},
                "plan.schema_invalid",
            )
            if rejected["reason_code"] not in _REJECTION_REASONS:
                raise ValidationFailure("plan.schema_invalid")
            _boolean(rejected["needs_human_review"], "plan.schema_invalid")
            _string(rejected["human_review_reason"], "plan.schema_invalid")
        initial = ranges["initial_range"]
        final = ranges["final_range"]
        if remedy["status"] == "not_needed" and (
            suggestion
            or requested_start is not None
            or initial != final
        ):
            raise ValidationFailure("plan.schema_invalid")
        if remedy["status"] == "applied" and (
            requested_start is None
            or requested_end is None
            or (requested_start, requested_end) != final
            or final[0] > initial[0]
            or final[1] < initial[1]
            or initial == final
        ):
            raise ValidationFailure("plan.schema_invalid")
        if remedy["status"] == "invalid" and initial != final:
            raise ValidationFailure("plan.schema_invalid")
    if len(published_ids) != published_count:
        raise ValidationFailure("plan.reference_invalid")
    return plan, candidate_ids, published_ids


def _validate_metadata(
    value: Any,
    *,
    run_id: str,
    candidate_ids: set[str],
    published_ids: set[str],
) -> tuple[dict[str, Any], dict[str, int]]:
    metadata = _object(value, _METADATA_FIELDS, "metadata.schema_invalid")
    if (
        metadata["schema_version"] != "short_video_catalog.v1"
        or metadata["run_id"] != run_id
        or metadata["result_kind"] not in {"clips", "empty"}
    ):
        raise ValidationFailure("metadata.schema_invalid")
    videos = _array(metadata["short_videos"], "metadata.schema_invalid")
    durations: dict[str, int] = {}
    actual_ids: set[str] = set()
    for raw_video in videos:
        video = _object(raw_video, _SHORT_VIDEO_FIELDS, "metadata.schema_invalid")
        short_video_id = _identifier(
            video["short_video_id"], "short_video", "metadata.schema_invalid"
        )
        candidate_id = _identifier(
            video["source_candidate_id"], "candidate", "metadata.schema_invalid"
        )
        if short_video_id in actual_ids or candidate_id not in candidate_ids:
            raise ValidationFailure("metadata.reference_invalid")
        actual_ids.add(short_video_id)
        start = _integer(video["start_ms"], "metadata.schema_invalid")
        end = _integer(video["end_ms"], "metadata.schema_invalid", positive=True)
        duration = _integer(video["duration_ms"], "metadata.schema_invalid", positive=True)
        if end - start != duration:
            raise ValidationFailure("metadata.schema_invalid")
        for name in ("topic_name", "title", "summary"):
            _string(video[name], "metadata.schema_invalid", nonempty=True)
        keywords = _array(video["keywords"], "metadata.schema_invalid")
        if any(
            not _string(keyword, "metadata.schema_invalid").strip()
            for keyword in keywords
        ):
            raise ValidationFailure("metadata.schema_invalid")
        media = _object(
            video["media"],
            {"audio_required", "container", "path", "video_required"},
            "metadata.schema_invalid",
        )
        expected_path = f"clips/{short_video_id}.mp4"
        if (
            _safe_path(media["path"]) != expected_path
            or media["container"] != "mp4"
            or media["video_required"] is not True
            or media["audio_required"] is not True
        ):
            raise ValidationFailure("metadata.schema_invalid")
        if video["subtitles"] != {"kind": "burned_in"}:
            raise ValidationFailure("metadata.schema_invalid")
        durations[expected_path] = duration
    if actual_ids != published_ids:
        raise ValidationFailure("metadata.reference_invalid")
    series_ids: set[str] = set()
    for raw_series in _array(metadata["series"], "metadata.schema_invalid"):
        series = _object(
            raw_series,
            {"series_id", "short_video_ids", "topic"},
            "metadata.schema_invalid",
        )
        series_id = _identifier(
            series["series_id"],
            "series",
            "metadata.schema_invalid",
        )
        if series_id in series_ids:
            raise ValidationFailure("metadata.reference_invalid")
        series_ids.add(series_id)
        _string(series["topic"], "metadata.schema_invalid", nonempty=True)
        ids = [
            _identifier(identifier, "short_video", "metadata.schema_invalid")
            for identifier in _array(
                series["short_video_ids"],
                "metadata.schema_invalid",
            )
        ]
        if (
            len(ids) < 2
            or len(ids) != len(set(ids))
            or any(identifier not in published_ids for identifier in ids)
        ):
            raise ValidationFailure("metadata.reference_invalid")
    return metadata, durations


def _candidate_order(candidate: Mapping[str, Any]) -> tuple[int, int, str]:
    final_range = candidate["final_range"]
    return (
        final_range["start_ms"],
        final_range["end_ms"],
        candidate["candidate_id"],
    )


def _canonical_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _expected_series(plan: Mapping[str, Any]) -> set[tuple[str, tuple[str, ...]]]:
    expected: set[tuple[str, tuple[str, ...]]] = set()
    run_topic = ""
    run_ids: list[str] = []

    def flush() -> None:
        if len(run_ids) >= 2:
            expected.add((run_topic, tuple(run_ids)))

    for candidate in sorted(plan["candidates"], key=_candidate_order):
        selection = candidate["selection"]
        if selection["outcome"] != "published":
            flush()
            run_topic = ""
            run_ids = []
            continue
        topic = _canonical_text(candidate["review"]["topic_name"])
        if run_ids and topic != run_topic:
            flush()
            run_ids = []
        if not run_ids:
            run_topic = topic
        run_ids.append(selection["short_video_id"])
    flush()
    return expected


def _validate_cross_references(
    *,
    manifest: Mapping[str, Any],
    transcript: Mapping[str, Any],
    plan: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    source_duration_ms = manifest["source"]["duration_ms"]
    candidate_by_id = {
        candidate["candidate_id"]: candidate
        for candidate in plan["candidates"]
    }
    published_by_candidate: dict[str, str] = {}
    for candidate in plan["candidates"]:
        for name in ("initial_range", "final_range"):
            if candidate[name]["end_ms"] > source_duration_ms:
                raise ValidationFailure("plan.reference_invalid")
        remedy = candidate["boundary_remedy"]
        review = candidate["review"]
        if (
            remedy["suggestion"] != review["boundary_fix_suggestion"]
            or remedy["requested_start_ms"] != review["boundary_fix_start_ms"]
            or remedy["requested_end_ms"] != review["boundary_fix_end_ms"]
        ):
            raise ValidationFailure("plan.reference_invalid")
        selection = candidate["selection"]
        if selection["outcome"] != "published":
            continue
        if (
            review["export_decision"] != "publish_ready"
            or review["topic_complete"] is not True
            or review["needs_human_review"] is not False
            or review["reject_reason"]
            or remedy["status"] == "invalid"
        ):
            raise ValidationFailure("plan.reference_invalid")
        published_by_candidate[candidate["candidate_id"]] = selection[
            "short_video_id"
        ]

    ordered_candidates = sorted(plan["candidates"], key=_candidate_order)
    candidate_positions = {
        candidate["candidate_id"]: index
        for index, candidate in enumerate(ordered_candidates)
    }
    video_by_id: dict[str, Mapping[str, Any]] = {}
    video_by_source: dict[str, str] = {}
    source_positions: dict[str, int] = {}
    for video in metadata["short_videos"]:
        short_video_id = video["short_video_id"]
        source_candidate_id = video["source_candidate_id"]
        candidate = candidate_by_id.get(source_candidate_id)
        if candidate is None or source_candidate_id in video_by_source:
            raise ValidationFailure("metadata.reference_invalid")
        review = candidate["review"]
        final_range = candidate["final_range"]
        if (
            video["topic_name"] != review["topic_name"]
            or video["title"] != review["title"]
            or video["summary"] != review["summary"]
            or video["keywords"] != review["keywords"]
            or video["start_ms"] != final_range["start_ms"]
            or video["end_ms"] != final_range["end_ms"]
            or video["end_ms"] > source_duration_ms
            or video["media"]["path"]
            != f"clips/{short_video_id}.mp4"
        ):
            raise ValidationFailure("metadata.reference_invalid")
        video_by_id[short_video_id] = video
        video_by_source[source_candidate_id] = short_video_id
        source_positions[short_video_id] = candidate_positions[source_candidate_id]
    if video_by_source != published_by_candidate:
        raise ValidationFailure("metadata.reference_invalid")
    manifest_media = {
        artifact["path"]
        for artifact in manifest["files"]
        if artifact["role"] == "short_video_media"
    }
    metadata_media = {
        video["media"]["path"] for video in metadata["short_videos"]
    }
    if manifest_media != metadata_media:
        raise ValidationFailure("metadata.reference_invalid")

    assigned: set[str] = set()
    actual_series: set[tuple[str, tuple[str, ...]]] = set()
    for series in metadata["series"]:
        member_ids = series["short_video_ids"]
        if (
            any(identifier not in video_by_id for identifier in member_ids)
            or assigned.intersection(member_ids)
        ):
            raise ValidationFailure("metadata.reference_invalid")
        canonical_topic = _canonical_text(series["topic"])
        if {
            _canonical_text(video_by_id[identifier]["topic_name"])
            for identifier in member_ids
        } != {canonical_topic}:
            raise ValidationFailure("metadata.reference_invalid")
        positions = tuple(source_positions[identifier] for identifier in member_ids)
        if positions != tuple(range(positions[0], positions[0] + len(positions))):
            raise ValidationFailure("metadata.reference_invalid")
        assigned.update(member_ids)
        actual_series.add((canonical_topic, tuple(member_ids)))
    if actual_series != _expected_series(plan):
        raise ValidationFailure("metadata.reference_invalid")


def _render_report(
    manifest: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> bytes:
    if manifest["result_kind"] == "empty":
        outcome = (
            "本次运行成功完成，形成有效空结果；"
            "没有候选满足发布条件，短视频集合为空。"
        )
    else:
        outcome = f"本次运行成功完成，共形成 {plan['published_count']} 条短视频。"
    return (
        "# 直播拆条报告\n\n"
        f"- 运行标识：`{manifest['run_id']}`\n"
        f"- 结果类型：`{manifest['result_kind']}`\n"
        f"- 候选数量：{plan['candidate_count']}\n"
        f"- 发布数量：{plan['published_count']}\n\n"
        "## 结果说明\n\n"
        f"{outcome}\n"
    ).encode("utf-8")


def _srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _render_transcript(transcript: Mapping[str, Any]) -> bytes:
    return "".join(
        f"{index}\n{_srt_timestamp(chunk['start_ms'])} --> "
        f"{_srt_timestamp(chunk['end_ms'])}\n{chunk['text']}\n\n"
        for index, chunk in enumerate(transcript["chunks"], start=1)
    ).encode("utf-8")


def _validate_expected_transcript(
    expected: Any,
    transcript: Mapping[str, Any],
) -> None:
    value = _object(
        expected,
        {"chunks", "schema_version", "source_duration_ms", "speech_presence"},
        "transcript.expected_invalid",
    )
    if value["schema_version"] != _EXPECTED_TRANSCRIPT_SCHEMA:
        raise ValidationFailure("transcript.expected_invalid")
    observed = {
        "speech_presence": transcript["speech_presence"],
        "source_duration_ms": transcript["source_duration_ms"],
        "chunks": [
            {
                "start_ms": chunk["start_ms"],
                "end_ms": chunk["end_ms"],
                "text": chunk["text"],
            }
            for chunk in transcript["chunks"]
        ],
    }
    if {
        "speech_presence": value["speech_presence"],
        "source_duration_ms": value["source_duration_ms"],
        "chunks": value["chunks"],
    } != observed:
        raise ValidationFailure("transcript.not_faithful")


def _run_ffprobe(
    descriptor: int,
    *,
    show_entries: str,
    count_packets_and_frames: bool,
    reason: str,
) -> Mapping[str, Any]:
    command = ["ffprobe", "-v", "error"]
    if count_packets_and_frames:
        command.extend(("-count_packets", "-count_frames"))
    command.extend(
        (
            "-show_entries",
            show_entries,
            "-of",
            "json",
            f"/proc/self/fd/{descriptor}",
        )
    )
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        with (
            tempfile.TemporaryFile(mode="w+b") as standard_output,
            tempfile.TemporaryFile(mode="w+b") as standard_error,
        ):
            completed = subprocess.run(
                command,
                check=False,
                stdout=standard_output,
                stderr=standard_error,
                env={"LC_ALL": "C", "PATH": os.environ.get("PATH", os.defpath)},
                pass_fds=(descriptor,),
                stdin=subprocess.DEVNULL,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
            output_size = os.fstat(standard_output.fileno()).st_size
            error_size = os.fstat(standard_error.fileno()).st_size
            if (
                completed.returncode != 0
                or output_size == 0
                or output_size > _MAX_PROBE_OUTPUT_BYTES
                or error_size != 0
            ):
                raise ValueError
            standard_output.seek(0)
            output = standard_output.read(_MAX_PROBE_OUTPUT_BYTES + 1)
            if len(output) != output_size:
                raise ValueError
        payload = json.loads(
            output,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except ValidationFailure:
        raise
    except (
        OSError,
        subprocess.TimeoutExpired,
        UnicodeError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        RecursionError,
    ):
        raise ValidationFailure(reason) from None


def _probe_source_duration(descriptor: int) -> int:
    try:
        payload = _run_ffprobe(
            descriptor,
            show_entries="format=duration",
            count_packets_and_frames=False,
            reason="source.invalid",
        )
        format_fact = _object(
            payload["format"],
            {"duration"},
            "source.invalid",
        )
        duration = Decimal(_string(format_fact["duration"], "source.invalid"))
        duration_ms = int(
            (duration * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        )
        if not duration.is_finite() or duration_ms <= 0:
            raise ValueError
        return duration_ms
    except (KeyError, ValueError, TypeError, DecimalException, ValidationFailure):
        raise ValidationFailure("source.invalid") from None


def _probe_mp4(descriptor: int, expected_duration_ms: int) -> None:
    try:
        payload = _run_ffprobe(
            descriptor,
            show_entries=(
                "format=duration,format_name:"
                "stream=codec_type,nb_read_packets,nb_read_frames"
            ),
            count_packets_and_frames=True,
            reason="media.invalid",
        )
        if not {"format", "streams"} <= set(payload):
            raise ValueError
        format_fact = _object(
            payload["format"],
            {"duration", "format_name"},
            "media.invalid",
        )
        names = set(_string(format_fact["format_name"], "media.invalid").split(","))
        duration = Decimal(_string(format_fact["duration"], "media.invalid"))
        streams = _array(payload["streams"], "media.invalid")
        required_streams: dict[str, int] = {"audio": 0, "video": 0}
        for raw_stream in streams:
            stream = _object(
                raw_stream,
                {"codec_type", "nb_read_frames", "nb_read_packets"},
                "media.invalid",
            )
            codec_type = _string(stream["codec_type"], "media.invalid")
            if codec_type not in required_streams:
                continue
            for field in ("nb_read_packets", "nb_read_frames"):
                count = _string(stream[field], "media.invalid", nonempty=True)
                if not count.isascii() or not count.isdecimal() or int(count) <= 0:
                    raise ValueError
            required_streams[codec_type] += 1
        if (
            "mp4" not in names
            or not duration.is_finite()
            or duration <= 0
            or abs(duration * 1000 - Decimal(expected_duration_ms)) > 250
            or any(count < 1 for count in required_streams.values())
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError, DecimalException, ValidationFailure):
        raise ValidationFailure("media.invalid") from None


def _validate_media_file(
    tree: _DeliveryTree,
    path: str,
    entry: Mapping[str, Any],
    expected_duration_ms: int,
) -> None:
    descriptor = _open_tree_file(tree, path, "artifact.unreadable")
    expected = tree.file_facts[path]
    try:
        byte_length, digest = _hash_descriptor(
            descriptor,
            expected,
            "artifact.unreadable",
        )
        if byte_length != entry["byte_length"] or digest != entry["sha256"]:
            raise ValidationFailure("artifact.digest_mismatch")
        _probe_mp4(descriptor, expected_duration_ms)
        _assert_unchanged(descriptor, expected)
    finally:
        os.close(descriptor)


def validate_delivery(
    delivery: Path,
    expected_transcript_path: Path,
    source_path: Path,
    expected_application_version: str,
) -> dict[str, Any]:
    """验证一份已发布标准交付物并返回机器可读摘要。"""
    expected_version = _string(
        expected_application_version,
        "application.version_mismatch",
        nonempty=True,
    )
    if _VERSION.fullmatch(expected_version) is None:
        raise ValidationFailure("application.version_mismatch")

    tree = _open_delivery_tree(Path(delivery))
    source: _ExternalFile | None = None
    try:
        source = _open_external_file(Path(source_path), "source.invalid")
        manifest = _validate_manifest(
            _load_json_bytes(
                _read_tree_document(
                    tree,
                    "manifest.json",
                    "manifest.unreadable",
                ),
                reason="manifest.unreadable",
            )
        )
        if manifest["application_version"] != expected_version:
            raise ValidationFailure("application.version_mismatch")

        source_byte_length, source_digest = _hash_descriptor(
            source.descriptor,
            source.fact,
            "source.invalid",
            changed_reason="source.identity_mismatch",
        )
        source_duration_ms = _probe_source_duration(source.descriptor)
        _assert_unchanged(
            source.descriptor,
            source.fact,
            "source.identity_mismatch",
        )
        source_contract = manifest["source"]
        if (
            source_contract["byte_length"] != source_byte_length
            or source_contract["sha256"] != source_digest
            or source_contract["duration_ms"] != source_duration_ms
        ):
            raise ValidationFailure("source.identity_mismatch")

        artifacts = _validate_artifacts(tree, manifest)
        run_id = manifest["run_id"]
        transcript, transcript_chunk_ids = _validate_transcript(
            _load_json_bytes(
                artifacts.documents["transcript.json"],
                reason="transcript.unreadable",
            ),
            run_id=run_id,
            source_duration_ms=source_duration_ms,
        )
        plan, candidate_ids, published_ids = _validate_plan(
            _load_json_bytes(
                artifacts.documents["plan.json"],
                reason="plan.unreadable",
            ),
            run_id=run_id,
            transcript_id=transcript["transcript_id"],
            transcript_chunks=transcript_chunk_ids,
        )
        metadata, media_durations = _validate_metadata(
            _load_json_bytes(
                artifacts.documents["metadata.json"],
                reason="metadata.unreadable",
            ),
            run_id=run_id,
            candidate_ids=candidate_ids,
            published_ids=published_ids,
        )
        documents = manifest["documents"]
        if (
            documents["transcript"]["transcript_id"]
            != transcript["transcript_id"]
            or documents["transcript_rendering"]["transcript_id"]
            != transcript["transcript_id"]
            or documents["plan"]["plan_id"] != plan["plan_id"]
            or plan["transcript_id"] != transcript["transcript_id"]
        ):
            raise ValidationFailure("reference.invalid")
        _validate_cross_references(
            manifest=manifest,
            transcript=transcript,
            plan=plan,
            metadata=metadata,
        )
        result_kind = manifest["result_kind"]
        short_video_count = len(metadata["short_videos"])
        execution_counts = manifest["execution"]["subtitle_optimization"]
        media_count = len(artifacts.media_entries)
        published_selection_count = sum(
            candidate["selection"]["outcome"] == "published"
            for candidate in plan["candidates"]
        )
        if (
            plan["result_kind"] != result_kind
            or metadata["result_kind"] != result_kind
            or plan["published_count"] != published_selection_count
            or published_selection_count != short_video_count
            or execution_counts["short_video_count"] != short_video_count
            or media_count != short_video_count
            or (
                result_kind == "empty"
                and (
                    short_video_count
                    or metadata["series"]
                    or any(execution_counts.values())
                )
            )
            or (result_kind == "clips" and short_video_count < 1)
        ):
            raise ValidationFailure("result_kind.invalid")
        if set(media_durations) != set(artifacts.media_entries):
            raise ValidationFailure("metadata.reference_invalid")
        if artifacts.documents["transcript.srt"] != _render_transcript(transcript):
            raise ValidationFailure("transcript.rendering_mismatch")
        expected_transcript = _load_json_bytes(
            _read_external_document(
                Path(expected_transcript_path),
                "transcript.expected_invalid",
            ),
            reason="transcript.expected_invalid",
        )
        _validate_expected_transcript(expected_transcript, transcript)
        try:
            artifacts.documents["report.md"].decode("utf-8", errors="strict")
        except UnicodeError:
            raise ValidationFailure("report.invalid") from None
        if artifacts.documents["report.md"] != _render_report(manifest, plan):
            raise ValidationFailure("report.invalid")
        for media_path, duration_ms in sorted(media_durations.items()):
            _validate_media_file(
                tree,
                media_path,
                artifacts.media_entries[media_path],
                duration_ms,
            )

        _revalidate_external(source, "source.identity_mismatch")
        _revalidate_delivery_tree(tree)
        return {
            "artifact_count": artifacts.artifact_count,
            "checks": {
                "digests": True,
                "exact_file_set": True,
                "faithful_transcript": True,
                "mp4": True,
                "path_safety": True,
                "references": True,
                "schema": True,
            },
            "result_kind": result_kind,
            "run_id": run_id,
            "schema_version": _RESULT_SCHEMA,
            "short_video_count": short_video_count,
            "success": True,
        }
    finally:
        if source is not None:
            source.close()
        tree.close()


def _resolve_result_target(path: Path) -> _ResultTarget:
    destination = Path(path)
    name = destination.name
    try:
        encoded_name = os.fsencode(name)
        if (
            name in {"", ".", ".."}
            or b"\x00" in encoded_name
            or len(encoded_name) > 255
        ):
            raise ValueError
        parent = destination.parent.resolve(strict=True)
        parent_fd = os.open(parent, _OPEN_DIRECTORY_FLAGS)
        try:
            parent_fact = _directory_fact(
                os.fstat(parent_fd),
                "result.path_invalid",
            )
            try:
                target_status = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                _regular_file_fact(target_status, "result.path_invalid")
        finally:
            os.close(parent_fd)
        return _ResultTarget(
            parent=parent,
            name=name,
            parent_fact=parent_fact,
        )
    except ValidationFailure:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise ValidationFailure("result.path_invalid") from None


def _resolve_delivery_path(path: Path) -> Path:
    try:
        supplied_status = Path(path).lstat()
        if stat.S_ISLNK(supplied_status.st_mode) or not stat.S_ISDIR(
            supplied_status.st_mode
        ):
            raise ValueError
        resolved = Path(path).resolve(strict=True)
        resolved_status = resolved.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(resolved_status.st_mode)
            or (resolved_status.st_dev, resolved_status.st_ino)
            != (supplied_status.st_dev, supplied_status.st_ino)
        ):
            raise ValueError
        return resolved
    except (OSError, RuntimeError, ValueError):
        raise ValidationFailure("delivery.invalid") from None


def _resolve_source_path(path: Path) -> Path:
    try:
        supplied_status = Path(path).lstat()
        if stat.S_ISLNK(supplied_status.st_mode) or not stat.S_ISREG(
            supplied_status.st_mode
        ):
            raise ValueError
        resolved = Path(path).resolve(strict=True)
        resolved_status = resolved.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(resolved_status.st_mode)
            or (resolved_status.st_dev, resolved_status.st_ino)
            != (supplied_status.st_dev, supplied_status.st_ino)
        ):
            raise ValueError
        return resolved
    except (OSError, RuntimeError, ValueError):
        raise ValidationFailure("source.invalid") from None


def _write_result(target: _ResultTarget, value: Mapping[str, Any]) -> None:
    contents = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    parent_fd = os.open(target.parent, _OPEN_DIRECTORY_FLAGS)
    temporary_name: str | None = None
    descriptor = -1
    try:
        observed_parent = _directory_fact(
            os.fstat(parent_fd),
            "result.path_invalid",
        )
        if observed_parent[:3] != target.parent_fact[:3]:
            raise OSError("result parent changed")
        temporary_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for _ in range(128):
            candidate = f".delivery-validation-{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(
                    candidate,
                    temporary_flags,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        else:
            raise OSError("cannot allocate result temporary file")
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short result write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            target.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_name = None
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def _emit_failure(target: _ResultTarget, reason_code: str) -> int:
    try:
        _write_result(
            target,
            {
                "reason_code": reason_code,
                "schema_version": _RESULT_SCHEMA,
                "success": False,
            },
        )
    except (OSError, ValidationFailure):
        print("独立交付校验失败：result.write_failed", file=sys.stderr)
        return 1
    print(f"独立交付校验失败：{reason_code}", file=sys.stderr)
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="独立校验已发布标准交付物")
    parser.add_argument("--delivery", required=True, type=Path)
    parser.add_argument("--expected-transcript", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-application-version", required=True)
    parser.add_argument("--result", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result_target = _resolve_result_target(arguments.result)
    except ValidationFailure:
        print("独立交付校验失败：result.path_invalid", file=sys.stderr)
        return 1
    try:
        delivery_root = _resolve_delivery_path(arguments.delivery)
    except ValidationFailure as failure:
        return _emit_failure(result_target, failure.reason_code)
    result_destination = result_target.parent / result_target.name
    if result_destination == delivery_root or result_destination.is_relative_to(
        delivery_root
    ):
        print("独立交付校验失败：result.path_invalid", file=sys.stderr)
        return 1
    try:
        source_path = _resolve_source_path(arguments.source)
    except ValidationFailure as failure:
        return _emit_failure(result_target, failure.reason_code)
    try:
        result = validate_delivery(
            delivery_root,
            arguments.expected_transcript,
            source_path,
            arguments.expected_application_version,
        )
    except ValidationFailure as failure:
        return _emit_failure(result_target, failure.reason_code)
    except (RecursionError, TypeError):
        return _emit_failure(result_target, "validation.invalid")
    try:
        _write_result(result_target, result)
    except (OSError, ValidationFailure):
        print("独立交付校验失败：result.write_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
