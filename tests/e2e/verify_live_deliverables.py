#!/usr/bin/env python3
"""直播拆条标准交付物校验器。

只读 live 拆条产物目录（默认 ./out），按 CONTEXT.md「标准交付物」与现有 e2e
断言口径检查交付包是否齐全、自洽。全部通过打印 ``E2E PASS`` 退出 0；任一失败
打印 ``E2E FAIL: <原因>`` 退出 1。

字段口径来源（真实代码，非猜测）：
- plan.json: video_auto_editor/plan.py:build_plan
- metadata.json: video_auto_editor/export.py:_write_metadata / _clip_metadata
- 拆条报告.md: video_auto_editor/report.py:generate_live_report
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from video_auto_editor.config import CONFIG, merge_config_file


class VerifyError(Exception):
    """单条校验失败。"""


class SrtCue:
    """解析后的单条 SRT cue。"""

    def __init__(self, index, start, end, text_lines):
        self.index = index
        self.start = start
        self.end = end
        self.text_lines = text_lines

    @property
    def text(self):
        return "\n".join(self.text_lines)


_SRT_TIME_RE = re.compile(r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$")
_PUNCT = "，。！？、；：…,!?;:"
_FILLER_SPLIT_RE = re.compile(rf"[{re.escape(_PUNCT)}\s]+")


def _load_json(path):
    if not path.exists():
        raise VerifyError(f"缺少交付物 {path.name}")
    if path.stat().st_size == 0:
        raise VerifyError(f"{path.name} 为空")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VerifyError(f"{path.name} 不是合法 JSON：{exc.msg}") from exc
    except UnicodeDecodeError as exc:
        raise VerifyError(f"{path.name} 不是合法 UTF-8：{exc}") from exc


def _output_child_path(output_dir, raw_path, label):
    path = Path(str(raw_path))
    if path.is_absolute() or ".." in path.parts:
        raise VerifyError(f"{label} 必须是输出目录内的相对路径：{raw_path}")
    output_root = output_dir.resolve()
    resolved = (output_root / path).resolve()
    try:
        resolved.relative_to(output_root)
    except ValueError as exc:
        raise VerifyError(f"{label} 路径越界：{raw_path}") from exc
    return resolved


def _load_config(config_file):
    if not config_file:
        return dict(CONFIG)
    return merge_config_file(CONFIG, config_file)


def _parse_srt_time(value, label):
    match = _SRT_TIME_RE.match(value.strip())
    if not match:
        raise VerifyError(f"{label} 时间戳格式非法：{value!r}")
    hours, minutes, seconds, milliseconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def _parse_srt(path, label):
    if not path.exists():
        raise VerifyError(f"缺少 {label}")
    if path.stat().st_size == 0:
        raise VerifyError(f"{label} 为空")
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise VerifyError(f"{label} 不是合法 UTF-8：{exc}") from exc
    if not raw.strip():
        raise VerifyError(f"{label} 为空")

    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [block for block in re.split(r"\n\s*\n", normalized.strip()) if block.strip()]
    if not blocks:
        raise VerifyError(f"{label} 没有 SRT cue")

    cues = []
    previous_end = None
    for expected_index, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if len(lines) < 3:
            raise VerifyError(f"{label} 第 {expected_index} 个 cue 不完整")
        try:
            index = int(lines[0].strip())
        except ValueError as exc:
            raise VerifyError(f"{label} 第 {expected_index} 个 cue 序号不是整数") from exc
        if index != expected_index:
            raise VerifyError(f"{label} cue 序号应连续从 1 开始，实际第 {expected_index} 个为 {index}")

        timeline_parts = [part.strip() for part in lines[1].split("-->")]
        if len(timeline_parts) != 2:
            raise VerifyError(f"{label} 第 {expected_index} 个 cue 缺少合法时间轴行")
        start = _parse_srt_time(timeline_parts[0], f"{label} 第 {expected_index} 个 cue 起点")
        end = _parse_srt_time(timeline_parts[1], f"{label} 第 {expected_index} 个 cue 终点")
        if end <= start:
            raise VerifyError(f"{label} 第 {expected_index} 个 cue 终点必须大于起点")
        if previous_end is not None and start < previous_end - 0.001:
            raise VerifyError(f"{label} 第 {expected_index} 个 cue 时间轴倒退或重叠")

        text_lines = [line.strip() for line in lines[2:] if line.strip()]
        if not text_lines:
            raise VerifyError(f"{label} 第 {expected_index} 个 cue 文本为空")
        cues.append(SrtCue(index, start, end, text_lines))
        previous_end = end
    return cues


def _check_transcript_srt(output_dir):
    srt_path = output_dir / "transcript.srt"
    _parse_srt(srt_path, "transcript.srt")


def _check_plan(output_dir):
    plan = _load_json(output_dir / "plan.json")
    if plan.get("status") != "reviewed":
        raise VerifyError(f"plan.json status 应为 reviewed，实际为 {plan.get('status')!r}（评审未成功则不会真实导出）")
    if plan.get("dry_run") is not False:
        raise VerifyError("plan.json dry_run 应为 false（完整导出而非 dry-run）")
    if not plan.get("candidates"):
        raise VerifyError("plan.json candidates 为空")
    if not isinstance(plan.get("exports"), list) or not plan["exports"]:
        raise VerifyError("plan.json exports 为空")
    if not isinstance(plan.get("export_count"), int) or plan["export_count"] < 1:
        raise VerifyError(f"plan.json export_count 应 >= 1，实际为 {plan.get('export_count')!r}")
    for idx, item in enumerate(plan["exports"]):
        for field in ("video_path", "subtitle_path"):
            if not item.get(field):
                raise VerifyError(f"plan.json exports[{idx}] 缺少非空字段 {field}")
    return plan


_OPTIMIZED_STATUS = "已优化烧录"
_UNOPTIMIZED_PREFIX = "未优化"


def _check_clip_optimization_fields(clip, idx):
    optimized = clip.get("subtitle_optimized")
    if not isinstance(optimized, bool):
        raise VerifyError(
            f"metadata.json clips[{idx}] 的 subtitle_optimized 必须是布尔值，实际为 {optimized!r}"
        )
    note = clip.get("subtitle_optimization_note", "")
    if not isinstance(note, str):
        raise VerifyError(f"metadata.json clips[{idx}] 的 subtitle_optimization_note 必须是字符串")
    if optimized is False and not note.strip():
        raise VerifyError(
            f"metadata.json clips[{idx}] 字幕优化失败必须给出 subtitle_optimization_note 供人工复核"
        )


def _check_metadata(output_dir):
    metadata = _load_json(output_dir / "metadata.json")
    if metadata.get("status") != "reviewed":
        raise VerifyError(f"metadata.json status 应为 reviewed，实际为 {metadata.get('status')!r}")
    clips = metadata.get("clips")
    if not isinstance(clips, list) or not clips:
        raise VerifyError("metadata.json clips 为空")
    for idx, clip in enumerate(clips):
        for field in ("title", "summary", "output_path", "subtitle_path"):
            if not clip.get(field):
                raise VerifyError(f"metadata.json clips[{idx}] 缺少非空字段 {field}")
        clip_file = _output_child_path(output_dir, clip["output_path"], f"metadata.json clips[{idx}] output_path")
        if not clip_file.exists() or clip_file.stat().st_size == 0:
            raise VerifyError(f"metadata.json clips[{idx}] 指向的视频不存在或为空：{clip['output_path']}")
        subtitle_file = _output_child_path(output_dir, clip["subtitle_path"], f"metadata.json clips[{idx}] subtitle_path")
        if not subtitle_file.exists() or subtitle_file.stat().st_size == 0:
            raise VerifyError(f"metadata.json clips[{idx}] 指向的字幕不存在或为空：{clip['subtitle_path']}")
        _check_clip_optimization_fields(clip, idx)
    if metadata.get("export_count") != len(clips):
        raise VerifyError(
            f"metadata.json export_count({metadata.get('export_count')}) 与 clips 数({len(clips)})不一致"
        )
    return metadata


def _check_plan_metadata_consistency(plan, metadata):
    plan_video_paths = [item["video_path"] for item in plan["exports"]]
    plan_subtitle_paths = [item["subtitle_path"] for item in plan["exports"]]
    metadata_video_paths = [clip["output_path"] for clip in metadata["clips"]]
    metadata_subtitle_paths = [clip["subtitle_path"] for clip in metadata["clips"]]

    if plan_video_paths != metadata_video_paths:
        raise VerifyError("plan.json exports[].video_path 与 metadata.json clips[].output_path 不一致")
    if plan_subtitle_paths != metadata_subtitle_paths:
        raise VerifyError("plan.json exports[].subtitle_path 与 metadata.json clips[].subtitle_path 不一致")


def _relative_file_set(output_dir, folder, pattern):
    root = output_dir / folder
    if not root.is_dir():
        return set()
    return {path.relative_to(output_dir).as_posix() for path in root.glob(pattern)}


def _format_path_set(paths):
    if not paths:
        return "[]"
    return "[" + ", ".join(sorted(paths)) + "]"


def _check_clip_files(output_dir, metadata):
    clip_files = sorted((output_dir / "clips").glob("*.mp4")) if (output_dir / "clips").exists() else []
    if not clip_files:
        raise VerifyError("clips/ 下没有导出的 .mp4")
    for clip_file in clip_files:
        if clip_file.stat().st_size == 0:
            raise VerifyError(f"导出的短视频为空：{clip_file.name}")

    subtitle_files = sorted((output_dir / "subtitles").glob("*.srt")) if (output_dir / "subtitles").exists() else []
    if not subtitle_files:
        raise VerifyError("subtitles/ 下没有导出的 .srt")

    expected_clips = {clip["output_path"] for clip in metadata["clips"]}
    expected_subtitles = {clip["subtitle_path"] for clip in metadata["clips"]}
    actual_clips = _relative_file_set(output_dir, "clips", "*.mp4")
    actual_subtitles = _relative_file_set(output_dir, "subtitles", "*.srt")

    if actual_clips != expected_clips:
        raise VerifyError(
            "clips/ 文件集合与 metadata.json 不一致："
            f"expected={_format_path_set(expected_clips)} actual={_format_path_set(actual_clips)}"
        )
    if actual_subtitles != expected_subtitles:
        raise VerifyError(
            "subtitles/ 文件集合与 metadata.json 不一致："
            f"expected={_format_path_set(expected_subtitles)} actual={_format_path_set(actual_subtitles)}"
        )


def _positive_int_config(config, key):
    value = config.get(key)
    if type(value) is not int or value <= 0:
        raise VerifyError(f"配置项 {key} 必须是正整数，实际为 {value!r}")
    return value


def _clip_buffered_duration(clip, config, idx):
    try:
        start = float(clip["start"])
        end = float(clip["end"])
        buffer_start = float(config.get("buffer_start", 0))
        buffer_end = float(config.get("buffer_end", 0))
    except (TypeError, ValueError, KeyError) as exc:
        raise VerifyError(f"metadata.json clips[{idx}] 缺少合法 start/end 或缓冲配置") from exc
    clip_start = max(0.0, start - buffer_start)
    clip_end = end + buffer_end
    if clip_end <= clip_start:
        raise VerifyError(f"metadata.json clips[{idx}] 计算后的导出时长不合法")
    return clip_end - clip_start


def _find_standalone_filler(text, filler_words):
    filler_set = {str(word) for word in filler_words if str(word)}
    single_char_fillers = {word for word in filler_set if len(word) == 1}
    if not filler_set:
        return ""
    for token in _FILLER_SPLIT_RE.split(text):
        stripped = token.strip()
        if not stripped:
            continue
        if stripped in filler_set:
            return stripped
        if single_char_fillers and all(char in single_char_fillers for char in stripped):
            return stripped
    return ""


def _check_subtitle_files(output_dir, metadata, config):
    if not config.get("export_subtitles", True):
        raise VerifyError("标准 e2e 要求 export_subtitles=true，以覆盖短视频字幕产物")

    max_chars = _positive_int_config(config, "subtitle_max_chars_per_line")
    max_lines = _positive_int_config(config, "subtitle_max_lines")
    filler_words = config.get("filler_words") or []
    if not isinstance(filler_words, list):
        raise VerifyError("配置项 filler_words 必须是 list")

    for idx, clip in enumerate(metadata["clips"]):
        subtitle_file = _output_child_path(output_dir, clip["subtitle_path"], f"metadata.json clips[{idx}] subtitle_path")
        cues = _parse_srt(subtitle_file, f"字幕文件 {clip['subtitle_path']}")
        buffered_duration = _clip_buffered_duration(clip, config, idx)
        if cues[-1].end > buffered_duration + 0.1:
            raise VerifyError(
                f"字幕文件 {clip['subtitle_path']} 时间轴超过导出片段时长："
                f"last_end={cues[-1].end:.3f}s duration={buffered_duration:.3f}s"
            )

        for cue in cues:
            if len(cue.text_lines) > max_lines:
                raise VerifyError(
                    f"字幕文件 {clip['subtitle_path']} cue {cue.index} 超过 subtitle_max_lines={max_lines}"
                )
            for line in cue.text_lines:
                if len(line) > max_chars:
                    raise VerifyError(
                        f"字幕文件 {clip['subtitle_path']} cue {cue.index} 字幕行超过 "
                        f"subtitle_max_chars_per_line={max_chars}：{line!r}"
                    )
            standalone_filler = _find_standalone_filler(cue.text, filler_words)
            if standalone_filler:
                raise VerifyError(
                    f"字幕文件 {clip['subtitle_path']} cue {cue.index} 仍包含纯语气词：{standalone_filler!r}"
                )


def _expected_burn_status(config):
    if not config.get("export_subtitles", True):
        return "关（未导出字幕）"
    if config.get("burn_subtitles", True):
        return "开（白字黑描边·底部居中）"
    return "关（仅旁挂 SRT）"


def _check_report(output_dir, config):
    report_path = output_dir / "拆条报告.md"
    if not report_path.exists():
        raise VerifyError("缺少 拆条报告.md")
    report = report_path.read_text(encoding="utf-8")
    if not report.strip():
        raise VerifyError("拆条报告.md 为空")
    if "非 dry-run 交付包" not in report:
        raise VerifyError("拆条报告.md 缺少『非 dry-run 交付包』段落（疑似走了 dry-run 或未评审路径）")
    if "| `metadata.json` | yes |" not in report:
        raise VerifyError("拆条报告.md 交付物清单缺少 `| `metadata.json` | yes |` 行")
    expected_burn_status = _expected_burn_status(config)
    if f"- 字幕烧录: {expected_burn_status}" not in report:
        raise VerifyError(f"拆条报告.md 字幕烧录状态应为：{expected_burn_status}")
    return report


def _extract_report_section(report, heading):
    """返回 ``heading`` 段落正文（到下一个 ``## `` 标题或文末）；段落不存在返回 None。"""
    collected = []
    capturing = False
    for line in report.splitlines():
        if line.strip() == heading:
            capturing = True
            continue
        if capturing and line.startswith("## "):
            break
        if capturing:
            collected.append(line)
    return "\n".join(collected) if capturing else None


def _parse_markdown_table_rows(section):
    """解析 Markdown 表格数据行，跳过分隔行（``|---|``）；按未转义竖线切分单元格。"""
    rows = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped)[1:-1]]
        if not cells:
            continue
        if all(cell and set(cell) <= {"-", ":"} for cell in cells):
            continue
        rows.append(cells)
    return rows


def _check_subtitle_optimization_report(metadata, report):
    """非 dry-run 交付包应逐条标注字幕优化状态，且与 metadata 计数一致。"""
    section = _extract_report_section(report, "## 字幕优化")
    if section is None:
        raise VerifyError("拆条报告.md 缺少『## 字幕优化』段落（非 dry-run 交付包应逐条标注字幕优化状态）")

    rows = [row for row in _parse_markdown_table_rows(section) if row[-1] != "字幕优化状态"]
    clips = metadata["clips"]
    if len(rows) != len(clips):
        raise VerifyError(
            f"拆条报告.md 字幕优化表行数({len(rows)})与 metadata.json clips 数({len(clips)})不一致"
        )

    report_optimized = 0
    report_unoptimized = 0
    for row in rows:
        status = row[-1]
        if status == _OPTIMIZED_STATUS:
            report_optimized += 1
        elif status.startswith(_UNOPTIMIZED_PREFIX):
            report_unoptimized += 1
        else:
            raise VerifyError(f"拆条报告.md 字幕优化状态文案不合法：{status!r}")

    metadata_optimized = sum(1 for clip in clips if clip.get("subtitle_optimized"))
    metadata_unoptimized = len(clips) - metadata_optimized
    if report_optimized != metadata_optimized or report_unoptimized != metadata_unoptimized:
        raise VerifyError(
            "拆条报告.md 字幕优化状态计数与 metadata.json 不一致："
            f"报告(已优化={report_optimized}, 未优化={report_unoptimized}) "
            f"metadata(已优化={metadata_optimized}, 未优化={metadata_unoptimized})"
        )


def verify(output_dir, config=None):
    output_dir = Path(output_dir)
    config = dict(CONFIG if config is None else config)
    if not output_dir.is_dir():
        raise VerifyError(f"输出目录不存在：{output_dir}")
    _check_transcript_srt(output_dir)
    plan = _check_plan(output_dir)
    metadata = _check_metadata(output_dir)
    _check_plan_metadata_consistency(plan, metadata)
    _check_clip_files(output_dir, metadata)
    _check_subtitle_files(output_dir, metadata, config)
    report = _check_report(output_dir, config)
    _check_subtitle_optimization_report(metadata, report)


def main(argv=None):
    parser = argparse.ArgumentParser(description="校验 live e2e 标准交付物")
    parser.add_argument("output_dir", nargs="?", default="out", help="live 输出目录，默认 out")
    parser.add_argument("--config-file", help="用于读取字幕配置的 JSON 配置文件")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        verify(args.output_dir, config=_load_config(args.config_file))
    except (VerifyError, ValueError) as exc:
        print(f"E2E FAIL: {exc}")
        return 1
    print(f"E2E PASS: {args.output_dir} 交付物齐全且字幕契约自洽")
    return 0


if __name__ == "__main__":
    sys.exit(main())
