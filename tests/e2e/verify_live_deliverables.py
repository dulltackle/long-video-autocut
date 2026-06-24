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

import json
import sys
from pathlib import Path


class VerifyError(Exception):
    """单条校验失败。"""


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


def _check_transcript_srt(output_dir):
    srt_path = output_dir / "transcript.srt"
    if not srt_path.exists():
        raise VerifyError("缺少 transcript.srt")
    text = srt_path.read_text(encoding="utf-8")
    if not text.strip():
        raise VerifyError("transcript.srt 为空")
    if "-->" not in text:
        raise VerifyError("transcript.srt 缺少 SRT 时间轴行（'-->'）")


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
    return plan


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
    if metadata.get("export_count") != len(clips):
        raise VerifyError(
            f"metadata.json export_count({metadata.get('export_count')}) 与 clips 数({len(clips)})不一致"
        )
    return metadata


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

    clips_in_metadata = len(metadata.get("clips", []))
    if len(clip_files) < clips_in_metadata or len(subtitle_files) < clips_in_metadata:
        raise VerifyError(
            "交付物缺失："
            f"metadata.clips={clips_in_metadata}、clips/*.mp4={len(clip_files)}、subtitles/*.srt={len(subtitle_files)}"
        )


def _check_report(output_dir):
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


def verify(output_dir):
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        raise VerifyError(f"输出目录不存在：{output_dir}")
    _check_transcript_srt(output_dir)
    _check_plan(output_dir)
    metadata = _check_metadata(output_dir)
    _check_clip_files(output_dir, metadata)
    _check_report(output_dir)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    output_dir = argv[0] if argv else "out"
    try:
        verify(output_dir)
    except VerifyError as exc:
        print(f"E2E FAIL: {exc}")
        return 1
    print(f"E2E PASS: {output_dir} 交付物齐全且自洽")
    return 0


if __name__ == "__main__":
    sys.exit(main())
