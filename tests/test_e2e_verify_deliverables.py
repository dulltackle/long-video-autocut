import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parent / "e2e" / "verify_live_deliverables.py"
SPEC = importlib.util.spec_from_file_location("verify_live_deliverables", MODULE_PATH)
verify_live_deliverables = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_live_deliverables)


def write_standard_deliverables(output_dir):
    output_dir.mkdir(exist_ok=True)
    (output_dir / "clips").mkdir()
    (output_dir / "subtitles").mkdir()
    (output_dir / "transcript.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n直播文本\n\n",
        encoding="utf-8",
    )
    (output_dir / "clips" / "001.mp4").write_bytes(b"clip")
    (output_dir / "subtitles" / "001.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n直播文本\n\n",
        encoding="utf-8",
    )
    (output_dir / "plan.json").write_text(
        json.dumps(
            {
                "status": "reviewed",
                "dry_run": False,
                "candidates": [{"candidate_index": 0}],
                "exports": [{"index": 1}],
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
                        "title": "直播标题",
                        "summary": "直播摘要",
                        "output_path": "clips/001.mp4",
                        "subtitle_path": "subtitles/001.srt",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (output_dir / "拆条报告.md").write_text(
        "## 非 dry-run 交付包\n\n| `metadata.json` | yes |\n",
        encoding="utf-8",
    )


def test_verify_accepts_extra_clip_and_subtitle_files(tmp_path):
    write_standard_deliverables(tmp_path)
    (tmp_path / "clips" / "old.mp4").write_bytes(b"old")
    (tmp_path / "subtitles" / "old.srt").write_text("old", encoding="utf-8")

    verify_live_deliverables.verify(tmp_path)


def test_verify_reports_invalid_utf8_json_as_verify_error(tmp_path):
    write_standard_deliverables(tmp_path)
    (tmp_path / "metadata.json").write_bytes(b"\xff")

    with pytest.raises(verify_live_deliverables.VerifyError, match="不是合法 UTF-8"):
        verify_live_deliverables.verify(tmp_path)


def test_verify_rejects_metadata_output_path_outside_output_dir(tmp_path):
    write_standard_deliverables(tmp_path)
    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    metadata["clips"][0]["output_path"] = "/tmp/outside.mp4"
    (tmp_path / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(verify_live_deliverables.VerifyError, match="output_path 必须是输出目录内的相对路径"):
        verify_live_deliverables.verify(tmp_path)
