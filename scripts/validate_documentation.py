#!/usr/bin/env python3
"""校验生产决策图、文档链接与领域术语。"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


_LEGACY_ADR_STATUSES = {
    "0001-use-hybrid-topic-detection-for-live-clips.md": "Superseded",
    "0002-export-all-publish-ready-live-clips-by-default.md": "Accepted",
    "0003-use-stepaudio-as-default-transcription-provider.md": "Superseded",
    "0004-transcribe-long-live-videos-in-audio-shards.md": "Superseded",
    "0005-use-stepfun-chat-as-default-topic-review-model.md": "Amended",
    "0006-review-topic-candidates-in-neighboring-batches.md": "Accepted",
    "0007-stop-on-transcription-failure-but-continue-planning-without-topic-review.md": "Superseded",
    "0008-keep-video-processing-in-cli-and-use-skill-for-orchestration.md": "Amended",
    "0009-cache-live-processing-results-with-result-affecting-inputs.md": "Amended",
    "0010-burn-subtitles-and-filter-filler-words-for-clips.md": "Superseded",
    "0011-optimize-clip-subtitles-with-llm-under-subsequence-constraint.md": "Superseded",
    "0012-segment-subtitle-optimization-window-to-improve-subsequence-pass-rate.md": "Superseded",
    "0013-overlap-asr-shards-and-backfill-tail-coverage.md": "Superseded",
}

_PRODUCTION_ADR_STATUSES = {
    "0014-certify-linux-native-production-environment-and-reproducible-installation.md": "Accepted",
    "0015-converge-live-only-public-interface-and-managed-workspace.md": "Accepted",
    "0016-adopt-all-or-nothing-live-run-state-machine.md": "Accepted",
    "0017-adopt-single-composition-root-and-deep-business-capability-modules.md": "Accepted",
    "0018-adopt-provider-agnostic-speech-recognition-and-coverage-ledger.md": "Accepted",
    "0019-adopt-shared-text-model-port-and-layered-topic-review.md": "Accepted",
    "0020-make-subtitle-optimization-and-burning-mandatory.md": "Accepted",
    "0021-adopt-versioned-content-addressed-processing-cache.md": "Accepted",
    "0022-adopt-versioned-standard-delivery-and-atomic-publication-after-verification.md": "Accepted",
    "0023-adopt-structured-run-diagnostics-and-stable-error-classification.md": "Accepted",
    "0024-unify-sensitive-data-provider-disclosure-and-local-retention-contract.md": "Accepted",
    "0025-approve-production-releases-with-layered-acceptance-evidence.md": "Accepted",
}

_APPROVED_LEGACY_RELATIONS = {
    "0001-use-hybrid-topic-detection-for-live-clips.md": (
        "Superseded by",
        "0019-adopt-shared-text-model-port-and-layered-topic-review.md",
    ),
    "0003-use-stepaudio-as-default-transcription-provider.md": (
        "Superseded by",
        "0018-adopt-provider-agnostic-speech-recognition-and-coverage-ledger.md",
    ),
    "0004-transcribe-long-live-videos-in-audio-shards.md": (
        "Superseded by",
        "0018-adopt-provider-agnostic-speech-recognition-and-coverage-ledger.md",
    ),
    "0005-use-stepfun-chat-as-default-topic-review-model.md": (
        "Amended by",
        "0019-adopt-shared-text-model-port-and-layered-topic-review.md",
    ),
    "0007-stop-on-transcription-failure-but-continue-planning-without-topic-review.md": (
        "Superseded by",
        "0016-adopt-all-or-nothing-live-run-state-machine.md",
    ),
    "0008-keep-video-processing-in-cli-and-use-skill-for-orchestration.md": (
        "Amended by",
        "0017-adopt-single-composition-root-and-deep-business-capability-modules.md",
    ),
    "0009-cache-live-processing-results-with-result-affecting-inputs.md": (
        "Amended by",
        "0021-adopt-versioned-content-addressed-processing-cache.md",
    ),
    "0010-burn-subtitles-and-filter-filler-words-for-clips.md": (
        "Superseded by",
        "0020-make-subtitle-optimization-and-burning-mandatory.md",
    ),
    "0011-optimize-clip-subtitles-with-llm-under-subsequence-constraint.md": (
        "Superseded by",
        "0020-make-subtitle-optimization-and-burning-mandatory.md",
    ),
    "0012-segment-subtitle-optimization-window-to-improve-subsequence-pass-rate.md": (
        "Superseded by",
        "0020-make-subtitle-optimization-and-burning-mandatory.md",
    ),
    "0013-overlap-asr-shards-and-backfill-tail-coverage.md": (
        "Superseded by",
        "0018-adopt-provider-agnostic-speech-recognition-and-coverage-ledger.md",
    ),
}

_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_REFERENCE_LINK_DEFINITION_RE = re.compile(
    r"^\s{0,3}\[([^\]]+)\]:\s*(?:<([^>]+)>|(\S+))(?:\s+.*)?$"
)
_REFERENCE_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\[([^\]]*)\]")
_SHORTCUT_REFERENCE_RE = re.compile(r"(?<![!\]])\[([^\]]+)\](?![\[(])")
_INLINE_CODE_RE = re.compile(r"(?P<ticks>`+).*?(?P=ticks)")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_RELATION_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+\s*)?$")
_RELATION_INVERSES = {
    "Amended by": "Amends",
    "Amends": "Amended by",
    "Superseded by": "Supersedes",
    "Supersedes": "Superseded by",
}
_RELATION_STATUS_RULES = {
    "Amended by": ("Amended", "Accepted"),
    "Amends": ("Accepted", "Amended"),
    "Superseded by": ("Superseded", "Accepted"),
    "Supersedes": ("Accepted", "Superseded"),
}
_ADR_METADATA_FIELDS = {"Status", "Date", *_RELATION_INVERSES}
_STALE_VOCABULARY_PATTERNS = (
    (
        "未评审导出",
        re.compile(
            r"(?<!不)(?<!不再)(?:用户)?显式允许未评审导出|"
            r"(?<!不)(?<!不再)允许未评审导出|"
            r"allow_unreviewed_export",
            re.IGNORECASE,
        ),
    ),
    (
        "字幕降级",
        re.compile(
            r"旁挂规则字幕|仅旁挂\s*SRT|回退规则字幕|"
            r"字幕优化失败.{0,30}旁挂规则\s*SRT|"
            r"字幕优化失败.{0,30}旁挂\s*SRT|"
            r"字幕优化失败.{0,30}(?:仍|继续).{0,10}导出|"
            r"不烧录字幕.{0,20}(?:仍|继续).{0,10}导出",
            re.IGNORECASE,
        ),
    ),
    (
        "旧处理缓存范围",
        re.compile(
            r"后续可扩展到主题评审|字幕优化缓存是独立命名空间|"
            r"不污染语音识别与主题评审缓存"
        ),
    ),
    (
        "旧运行诊断语义",
        re.compile(
            r"(?<!work/)runs/<run_id>/|所有正式直播拆条运行|"
            r"进程被强制终止时允许形成.{0,20}诊断记录"
        ),
    ),
)


@dataclass(frozen=True)
class Diagnostic:
    """一条可定位的文档契约诊断。"""

    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path.as_posix()}:{self.line}: {self.message}"


@dataclass(frozen=True)
class AdrRelation:
    """ADR 元数据中的一条有向关系。"""

    source: Path
    field: str
    target: Path
    label: str
    raw_target: str
    line: int


@dataclass(frozen=True)
class MarkdownLink:
    """Markdown 中解析后的有效链接引用。"""

    line: int
    label: str
    target: str | None
    reference_id: str | None = None


def _relative_path(root: Path, path: Path) -> Path:
    return path.relative_to(root)


def _adr_metadata(path: Path) -> list[tuple[int, str, str]]:
    metadata: list[tuple[int, str, str]] = []
    after_title = False
    started = False
    for line_number, line in _markdown_nonfenced_lines(path):
        if not after_title:
            if line.startswith("# "):
                after_title = True
            continue
        if not line.strip():
            if started:
                break
            continue
        field, separator, value = line.partition(":")
        if separator and field in _ADR_METADATA_FIELDS:
            started = True
            metadata.append((line_number, field, value.strip()))
            continue
        break
    return metadata


def _validate_expected_statuses(
    root: Path,
    expected_statuses: dict[str, str],
    *,
    group_name: str,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    adr_dir = root / "docs" / "adr"
    for filename, expected_status in expected_statuses.items():
        path = adr_dir / filename
        relative_path = _relative_path(root, path)
        if not path.is_file():
            diagnostics.append(Diagnostic(relative_path, 1, f"缺少 {group_name} 文件"))
            continue

        status_fields = [
            (line_number, value)
            for line_number, field, value in _adr_metadata(path)
            if field == "Status"
        ]
        if not status_fields:
            diagnostics.append(Diagnostic(relative_path, 1, f"{group_name} 缺少 Status 元数据"))
            continue
        if len(status_fields) != 1:
            diagnostics.append(
                Diagnostic(
                    relative_path,
                    status_fields[0][0],
                    f"Status 元数据必须恰好一条，实际为 {len(status_fields)} 条",
                )
            )
            continue

        line_number, actual_status = status_fields[0]
        if actual_status != expected_status:
            diagnostics.append(
                Diagnostic(
                    relative_path,
                    line_number,
                    f"{group_name} 状态应为 {expected_status}，实际为 {actual_status}",
                )
            )
    return diagnostics


def _validate_required_relation_targets(root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    adr_paths = {path.resolve() for path in (root / "docs" / "adr").glob("*.md")}
    for path in sorted(adr_paths):
        status_field = next(
            (
                (line_number, value)
                for line_number, field, value in _adr_metadata(path)
                if field == "Status"
            ),
            None,
        )
        if status_field is None or status_field[1] not in {"Amended", "Superseded"}:
            continue
        status = status_field[1]
        field = "Amended by" if status == "Amended" else "Superseded by"

        has_target = False
        for _line_number, relation_field, value in _adr_metadata(path):
            if relation_field != field:
                continue
            for match in _RELATION_LINK_RE.finditer(value):
                try:
                    parsed = urlsplit(match.group(2))
                except ValueError:
                    continue
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                target = (path.parent / unquote(parsed.path)).resolve()
                if (
                    target in adr_paths
                    and target != path
                    and _read_status(target) == "Accepted"
                ):
                    has_target = True
                    break
            if has_target:
                break
        if not has_target:
            diagnostics.append(
                Diagnostic(
                    _relative_path(root, path),
                    status_field[0],
                    f"{status} ADR 缺少有效的 {field} 目标",
                )
            )
    return diagnostics


def _markdown_nonfenced_lines(path: Path) -> list[tuple[int, str]]:
    content_lines: list[tuple[int, str]] = []
    fence_character: str | None = None
    fence_length = 0
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if fence_character is not None:
            if re.match(
                rf"^ {{0,3}}{re.escape(fence_character)}{{{fence_length},}}\s*$",
                line,
            ):
                fence_character = None
                fence_length = 0
            continue

        fence_match = _FENCE_OPEN_RE.match(line)
        if fence_match is not None:
            fence = fence_match.group(1)
            fence_character = fence[0]
            fence_length = len(fence)
            continue

        content_lines.append((line_number, line))
    return content_lines


def _markdown_content_lines(path: Path) -> list[tuple[int, str]]:
    return [
        (
            line_number,
            _INLINE_CODE_RE.sub(
                lambda match: " " * len(match.group(0)),
                line,
            ),
        )
        for line_number, line in _markdown_nonfenced_lines(path)
    ]


def _normalize_reference_id(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _iter_markdown_links(path: Path):
    content_lines = _markdown_content_lines(path)
    definitions: dict[str, str] = {}
    for _line_number, line in content_lines:
        match = _REFERENCE_LINK_DEFINITION_RE.match(line)
        if match is None:
            continue
        definitions[_normalize_reference_id(match.group(1))] = match.group(2) or match.group(3)

    for line_number, line in content_lines:
        if _REFERENCE_LINK_DEFINITION_RE.match(line) is not None:
            continue
        for match in _MARKDOWN_LINK_RE.finditer(line):
            yield MarkdownLink(
                line=line_number,
                label=match.group(1),
                target=match.group(2),
            )
        for match in _REFERENCE_LINK_RE.finditer(line):
            reference_id = match.group(2) or match.group(1)
            yield MarkdownLink(
                line=line_number,
                label=match.group(1),
                target=definitions.get(_normalize_reference_id(reference_id)),
                reference_id=reference_id,
            )
        for match in _SHORTCUT_REFERENCE_RE.finditer(line):
            reference_id = match.group(1)
            normalized_reference_id = _normalize_reference_id(reference_id)
            if normalized_reference_id not in definitions:
                continue
            yield MarkdownLink(
                line=line_number,
                label=reference_id,
                target=definitions[normalized_reference_id],
                reference_id=reference_id,
            )


def _heading_slug(title: str) -> str:
    title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title)
    title = title.replace("`", "").lower()
    normalized = "".join(
        character
        for character in title
        if character.isalnum() or character in {" ", "-", "_"}
    )
    return normalized.strip().replace(" ", "-")


def _markdown_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    for _line_number, line in _markdown_nonfenced_lines(path):
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        base_slug = _heading_slug(match.group(1))
        slug = base_slug
        duplicate_index = 1
        while slug in anchors:
            slug = f"{base_slug}-{duplicate_index}"
            duplicate_index += 1
        anchors.add(slug)
    return anchors


def _validate_local_link_paths(root: Path, paths: list[Path]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    anchor_cache: dict[Path, set[str]] = {}
    for source_path in paths:
        for link in _iter_markdown_links(source_path):
            if link.target is None:
                diagnostics.append(
                    Diagnostic(
                        _relative_path(root, source_path),
                        link.line,
                        f"引用定义不存在：[{link.reference_id}]",
                    )
                )
                continue
            raw_target = link.target
            target = raw_target.removeprefix("<").removesuffix(">")
            try:
                parsed = urlsplit(target)
            except ValueError:
                diagnostics.append(
                    Diagnostic(
                        _relative_path(root, source_path),
                        link.line,
                        f"链接目标格式非法：{raw_target}",
                    )
                )
                continue
            if parsed.scheme or parsed.netloc:
                continue

            relative_target = unquote(parsed.path)
            resolved_target = (
                source_path if not relative_target else source_path.parent / relative_target
            ).resolve()
            try:
                resolved_target.relative_to(root)
            except ValueError:
                diagnostics.append(
                    Diagnostic(
                        _relative_path(root, source_path),
                        link.line,
                        f"本地链接目标越出仓库：{raw_target}",
                    )
                )
                continue
            if not resolved_target.is_file():
                diagnostics.append(
                    Diagnostic(
                        _relative_path(root, source_path),
                        link.line,
                        f"本地链接目标不存在：{raw_target}",
                    )
                )
                continue
            if parsed.fragment:
                if resolved_target not in anchor_cache:
                    anchor_cache[resolved_target] = _markdown_anchors(resolved_target)
                anchors = anchor_cache[resolved_target]
                fragment = unquote(parsed.fragment)
                if fragment not in anchors:
                    diagnostics.append(
                        Diagnostic(
                            _relative_path(root, source_path),
                            link.line,
                            f"本地链接锚点不存在：#{fragment}",
                        )
                    )
    return diagnostics


def _collect_adr_relations(root: Path, paths: list[Path]) -> dict[Path, list[AdrRelation]]:
    relations: dict[Path, list[AdrRelation]] = {path.resolve(): [] for path in paths}
    for source_path in paths:
        for line_number, field, value in _adr_metadata(source_path):
            if field not in _RELATION_INVERSES:
                continue
            for match in _RELATION_LINK_RE.finditer(value):
                raw_target = match.group(2)
                try:
                    parsed = urlsplit(raw_target)
                except ValueError:
                    continue
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                target = (source_path.parent / unquote(parsed.path)).resolve()
                relations[source_path.resolve()].append(
                    AdrRelation(
                        source=source_path.resolve(),
                        field=field,
                        target=target,
                        label=match.group(1).strip(),
                        raw_target=raw_target,
                        line=line_number,
                    )
                )
    return relations


def _validate_relation_declarations(root: Path, paths: list[Path]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    adr_paths = {path.resolve() for path in paths}
    for source_path in paths:
        for line_number, field, value in _adr_metadata(source_path):
            if field not in _RELATION_INVERSES:
                continue
            matches = list(_RELATION_LINK_RE.finditer(value))
            well_formed = bool(matches)
            if matches:
                well_formed = not value[: matches[0].start()].strip()
                for previous, current in zip(matches, matches[1:]):
                    if re.fullmatch(r"\s*,\s*", value[previous.end() : current.start()]) is None:
                        well_formed = False
                if value[matches[-1].end() :].strip():
                    well_formed = False
            if not well_formed:
                diagnostics.append(
                    Diagnostic(
                        _relative_path(root, source_path),
                        line_number,
                        f"ADR 关系元数据格式非法：{field}: {value}",
                    )
                )
            for match in matches:
                raw_target = match.group(2)
                try:
                    parsed = urlsplit(raw_target)
                except ValueError:
                    continue
                target = (
                    (source_path.parent / unquote(parsed.path)).resolve()
                    if parsed.path
                    else None
                )
                if (
                    parsed.scheme
                    or parsed.netloc
                    or target is None
                    or target not in adr_paths
                ):
                    diagnostics.append(
                        Diagnostic(
                            _relative_path(root, source_path),
                            line_number,
                            "ADR 关系目标必须是 docs/adr 内的本地文件："
                            f"{raw_target}",
                        )
                    )
    return diagnostics


def _read_h1_title(path: Path) -> str | None:
    return next(
        (
            line.removeprefix("# ").strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("# ")
        ),
        None,
    )


def _read_status(path: Path) -> str | None:
    return next(
        (
            value
            for _line_number, field, value in _adr_metadata(path)
            if field == "Status"
        ),
        None,
    )


def _validate_relation_titles(
    root: Path,
    relations_by_source: dict[Path, list[AdrRelation]],
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for relations in relations_by_source.values():
        for relation in relations:
            if not relation.target.is_file():
                continue
            target_title = _read_h1_title(relation.target)
            if relation.label == target_title:
                continue
            diagnostics.append(
                Diagnostic(
                    _relative_path(root, relation.source),
                    relation.line,
                    f"关系链接标题“{relation.label}”与目标标题“{target_title or '缺失'}”不一致",
                )
            )
    return diagnostics


def _validate_relation_statuses(
    root: Path,
    relations_by_source: dict[Path, list[AdrRelation]],
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for relations in relations_by_source.values():
        for relation in relations:
            if not relation.target.is_file():
                continue
            expected_source_status, expected_target_status = _RELATION_STATUS_RULES[relation.field]
            source_status = _read_status(relation.source)
            target_status = _read_status(relation.target)
            if source_status != expected_source_status:
                diagnostics.append(
                    Diagnostic(
                        _relative_path(root, relation.source),
                        relation.line,
                        f"{relation.field} 源 ADR 状态必须为 {expected_source_status}，"
                        f"实际为 {source_status or '缺失'}",
                    )
                )
            if relation.target == relation.source or target_status != expected_target_status:
                diagnostics.append(
                    Diagnostic(
                        _relative_path(root, relation.source),
                        relation.line,
                        f"{relation.field} 目标必须是不同的 {expected_target_status} ADR："
                        f"{relation.raw_target} 的状态为 {target_status or '缺失'}",
                    )
                )
    return diagnostics


def _validate_relation_symmetry(
    root: Path,
    relations_by_source: dict[Path, list[AdrRelation]],
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for source_relations in relations_by_source.values():
        for relation in source_relations:
            if not relation.target.is_file():
                continue
            inverse_field = _RELATION_INVERSES[relation.field]
            inverse_exists = any(
                candidate.field == inverse_field and candidate.target == relation.source
                for candidate in relations_by_source.get(relation.target, [])
            )
            if inverse_exists:
                continue
            diagnostics.append(
                Diagnostic(
                    _relative_path(root, relation.source),
                    relation.line,
                    "关系不对称："
                    f"{relation.field} 指向 {relation.raw_target}，"
                    f"目标缺少 {inverse_field} 回指",
                )
            )
    return diagnostics


def _validate_approved_relation_graph(
    root: Path,
    relations_by_source: dict[Path, list[AdrRelation]],
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    adr_dir = root / "docs" / "adr"
    for source_filename, (expected_field, target_filename) in _APPROVED_LEGACY_RELATIONS.items():
        source = (adr_dir / source_filename).resolve()
        target = (adr_dir / target_filename).resolve()
        if not source.is_file():
            continue
        source_relations = relations_by_source.get(source, [])
        if not any(
            relation.field == expected_field and relation.target == target
            for relation in source_relations
        ):
            status_line = next(
                (
                    line_number
                    for line_number, field, _value in _adr_metadata(source)
                    if field == "Status"
                ),
                1,
            )
            diagnostics.append(
                Diagnostic(
                    _relative_path(root, source),
                    status_line,
                    f"决策图缺少批准的 {expected_field} 关系：{target_filename}",
                )
            )

        for relation in source_relations:
            if relation.field not in {"Amended by", "Superseded by"}:
                continue
            if relation.field == expected_field and relation.target == target:
                continue
            diagnostics.append(
                Diagnostic(
                    _relative_path(root, source),
                    relation.line,
                    f"决策图包含未批准的 {relation.field} 关系：{relation.raw_target}",
                )
            )
    return diagnostics


def _validate_production_spec_adr_titles(
    root: Path,
    production_spec_path: Path,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    adr_dir = (root / "docs" / "adr").resolve()
    for link in _iter_markdown_links(production_spec_path):
        if link.target is None:
            continue
        raw_target = link.target
        try:
            parsed = urlsplit(raw_target)
        except ValueError:
            continue
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        target = (production_spec_path.parent / unquote(parsed.path)).resolve()
        if target.parent != adr_dir or not target.is_file():
            continue
        target_title = _read_h1_title(target)
        if link.label.strip() == target_title:
            continue
        diagnostics.append(
            Diagnostic(
                _relative_path(root, production_spec_path),
                link.line,
                f"内部 ADR 引用标题“{link.label.strip()}”"
                f"与目标标题“{target_title or '缺失'}”不一致",
            )
        )
    return diagnostics


def _validate_domain_vocabulary(root: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    context_path = root / "CONTEXT.md"
    if not context_path.is_file():
        return [
            Diagnostic(
                _relative_path(root, context_path),
                1,
                "缺少领域文档",
            )
        ]
    for line_number, line in enumerate(context_path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("_Avoid_:"):
            continue
        for category, pattern in _STALE_VOCABULARY_PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            diagnostics.append(
                Diagnostic(
                    _relative_path(root, context_path),
                    line_number,
                    f"过期领域语义[{category}]：检测到“{match.group(0)}”；原文：{line.strip()}",
                )
            )
    return diagnostics


def validate_repository(root: Path) -> list[Diagnostic]:
    """返回仓库文档契约的全部诊断。"""

    root = root.resolve()
    adr_paths = sorted((root / "docs" / "adr").glob("*.md"))
    relations_by_source = _collect_adr_relations(root, adr_paths)
    production_spec_path = root / "docs" / "production-readiness-spec.md"
    source_paths = [*adr_paths]
    missing_document_diagnostics: list[Diagnostic] = []
    if production_spec_path.is_file():
        source_paths.append(production_spec_path)
    else:
        missing_document_diagnostics.append(
            Diagnostic(
                _relative_path(root, production_spec_path),
                1,
                "缺少生产就绪规格",
            )
        )
    diagnostics = [
        *missing_document_diagnostics,
        *_validate_expected_statuses(
            root,
            _LEGACY_ADR_STATUSES,
            group_name="旧 ADR",
        ),
        *_validate_expected_statuses(
            root,
            _PRODUCTION_ADR_STATUSES,
            group_name="新生产级 ADR",
        ),
        *_validate_required_relation_targets(root),
        *_validate_local_link_paths(root, source_paths),
        *_validate_relation_declarations(root, adr_paths),
        *_validate_relation_titles(root, relations_by_source),
        *_validate_relation_statuses(root, relations_by_source),
        *_validate_relation_symmetry(root, relations_by_source),
        *_validate_approved_relation_graph(root, relations_by_source),
        *(
            _validate_production_spec_adr_titles(root, production_spec_path)
            if production_spec_path.is_file()
            else []
        ),
        *_validate_domain_vocabulary(root),
    ]
    return sorted(
        diagnostics,
        key=lambda item: (item.path.as_posix(), item.line, item.message),
    )


def main(argv: list[str] | None = None) -> int:
    """运行仓库文档门禁。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="待校验的仓库根目录（默认当前脚本所在仓库）",
    )
    args = parser.parse_args(argv)

    diagnostics = validate_repository(args.root)
    if diagnostics:
        for diagnostic in diagnostics:
            print(diagnostic, file=sys.stderr)
        return 1

    print("文档校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
