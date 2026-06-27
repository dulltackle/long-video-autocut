import importlib.util
import json
from pathlib import Path

import pytest

from video_auto_editor.config import CONFIG


MODULE_PATH = Path(__file__).with_name("verify_live_deliverables.py")
SPEC = importlib.util.spec_from_file_location("verify_live_deliverables", MODULE_PATH)
verify_live_deliverables = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_live_deliverables)


def e2e_config(**overrides):
    config = CONFIG.copy()
    config.update({"buffer_start": 1, "buffer_end": 3})
    config.update(overrides)
    return config


def write_standard_deliverables(output_dir, *, subtitle_text=None, report_burn_status="开（白字黑描边·底部居中）"):
    output_dir.mkdir(exist_ok=True)
    (output_dir / "clips").mkdir()
    (output_dir / "subtitles").mkdir()
    (output_dir / "transcript.srt").write_text(
        "1\n"
        "00:00:10,000 --> 00:00:13,000\n"
        "嗯，直播原始转写保留语气词\n\n",
        encoding="utf-8",
    )
    (output_dir / "clips" / "001_直播标题.mp4").write_bytes(b"clip")
    (output_dir / "subtitles" / "001_直播标题.srt").write_text(
        subtitle_text
        or (
            "1\n"
            "00:00:01,000 --> 00:00:04,000\n"
            "今天我们讲愉悦\n"
            "技术\n\n"
        ),
        encoding="utf-8",
    )
    (output_dir / "plan.json").write_text(
        json.dumps(
            {
                "status": "reviewed",
                "dry_run": False,
                "candidates": [{"candidate_index": 0}],
                "exports": [
                    {
                        "export_index": 1,
                        "video_path": "clips/001_直播标题.mp4",
                        "subtitle_path": "subtitles/001_直播标题.srt",
                    }
                ],
                "export_count": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (output_dir / "metadata.json").write_text(
        json.dumps(
            {
                "status": "reviewed",
                "export_count": 1,
                "clips": [
                    {
                        "index": 1,
                        "title": "直播标题",
                        "summary": "直播摘要",
                        "start": 10,
                        "end": 20,
                        "duration": 10,
                        "output_path": "clips/001_直播标题.mp4",
                        "subtitle_path": "subtitles/001_直播标题.srt",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (output_dir / "拆条报告.md").write_text(
        "## 视频信息\n\n"
        f"- 字幕烧录: {report_burn_status}\n\n"
        "> Reviewed 非 dry-run 交付包：包含实际导出文件和字幕文件。\n\n"
        "## 标准交付物\n\n"
        "| `metadata.json` | yes |\n",
        encoding="utf-8",
    )


def test_verify_accepts_standard_delivery_with_subtitle_contract(tmp_path):
    write_standard_deliverables(tmp_path)

    verify_live_deliverables.verify(tmp_path, config=e2e_config())


def test_verify_accepts_sidecar_subtitles_when_burn_disabled(tmp_path):
    write_standard_deliverables(tmp_path, report_burn_status="关（仅旁挂 SRT）")

    verify_live_deliverables.verify(tmp_path, config=e2e_config(burn_subtitles=False))


def test_verify_reports_invalid_utf8_json_as_verify_error(tmp_path):
    write_standard_deliverables(tmp_path)
    (tmp_path / "metadata.json").write_bytes(b"\xff")

    with pytest.raises(verify_live_deliverables.VerifyError, match="不是合法 UTF-8"):
        verify_live_deliverables.verify(tmp_path, config=e2e_config())


def test_verify_rejects_metadata_output_path_outside_output_dir(tmp_path):
    write_standard_deliverables(tmp_path)
    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    metadata["clips"][0]["output_path"] = "/tmp/outside.mp4"
    (tmp_path / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(verify_live_deliverables.VerifyError, match="output_path 必须是输出目录内的相对路径"):
        verify_live_deliverables.verify(tmp_path, config=e2e_config())


def test_verify_rejects_stale_extra_clip_and_subtitle_files(tmp_path):
    write_standard_deliverables(tmp_path)
    (tmp_path / "clips" / "old.mp4").write_bytes(b"old")
    (tmp_path / "subtitles" / "old.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n旧字幕\n\n", encoding="utf-8")

    with pytest.raises(verify_live_deliverables.VerifyError, match="文件集合与 metadata.json 不一致"):
        verify_live_deliverables.verify(tmp_path, config=e2e_config())


def test_verify_rejects_plan_metadata_path_mismatch(tmp_path):
    write_standard_deliverables(tmp_path)
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    plan["exports"][0]["subtitle_path"] = "subtitles/002_直播标题.srt"
    (tmp_path / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(verify_live_deliverables.VerifyError, match="subtitle_path 不一致"):
        verify_live_deliverables.verify(tmp_path, config=e2e_config())


def test_verify_rejects_pure_filler_in_clip_subtitle(tmp_path):
    write_standard_deliverables(
        tmp_path,
        subtitle_text="1\n00:00:01,000 --> 00:00:04,000\n嗯\n\n",
    )

    with pytest.raises(verify_live_deliverables.VerifyError, match="仍包含纯语气词"):
        verify_live_deliverables.verify(tmp_path, config=e2e_config())


def test_verify_rejects_overlong_subtitle_line(tmp_path):
    write_standard_deliverables(
        tmp_path,
        subtitle_text="1\n00:00:01,000 --> 00:00:04,000\n" + ("甲" * 16) + "\n\n",
    )

    with pytest.raises(verify_live_deliverables.VerifyError, match="字幕行超过 subtitle_max_chars_per_line"):
        verify_live_deliverables.verify(tmp_path, config=e2e_config())


def test_verify_rejects_too_many_subtitle_lines(tmp_path):
    write_standard_deliverables(
        tmp_path,
        subtitle_text="1\n00:00:01,000 --> 00:00:04,000\n第一行\n第二行\n第三行\n\n",
    )

    with pytest.raises(verify_live_deliverables.VerifyError, match="超过 subtitle_max_lines"):
        verify_live_deliverables.verify(tmp_path, config=e2e_config())


def test_verify_rejects_subtitle_timeline_beyond_buffered_clip(tmp_path):
    write_standard_deliverables(
        tmp_path,
        subtitle_text="1\n00:00:13,000 --> 00:00:15,000\n超出片段\n\n",
    )

    with pytest.raises(verify_live_deliverables.VerifyError, match="时间轴超过导出片段时长"):
        verify_live_deliverables.verify(tmp_path, config=e2e_config())


def test_verify_uses_custom_subtitle_line_limit(tmp_path):
    write_standard_deliverables(
        tmp_path,
        subtitle_text="1\n00:00:01,000 --> 00:00:04,000\n" + ("甲" * 16) + "\n\n",
    )

    verify_live_deliverables.verify(tmp_path, config=e2e_config(subtitle_max_chars_per_line=20))


def test_main_loads_config_file_for_subtitle_contract(tmp_path, capsys):
    write_standard_deliverables(
        tmp_path,
        subtitle_text="1\n00:00:01,000 --> 00:00:04,000\n" + ("甲" * 16) + "\n\n",
    )
    config_path = tmp_path / "config.json"
    config_path.write_text('{"subtitle_max_chars_per_line": 20}', encoding="utf-8")

    assert verify_live_deliverables.main([str(tmp_path), "--config-file", str(config_path)]) == 0
    assert "E2E PASS" in capsys.readouterr().out
