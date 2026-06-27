"""全局默认配置与配置文件加载。"""

import json

CONFIG = {
    "silence_noise": -30,
    "silence_duration": 0.8,
    "min_score": 90,
    "min_duration": 15,
    "buffer_start": 1,
    "buffer_end": 3,
    "crf": 18,
    "preset": "fast",
    "audio_bitrate": "192k",
    "penalty_repeat": 5,
    "penalty_stutter": 3,
    "penalty_interrupt": 10,
    "bonus_natural_end": 5,
    "bonus_completeness_max": 3,
    "duplicate_threshold": 0.7,
    "min_clip_duration": 30,
    "max_clip_duration": 180,
    "target_clip_duration": 90,
    "topic_overlap_seconds": 15,
    "context_expand_before": 12,
    "context_expand_after": 8,
    "max_clips": 5,
    "temporary_protective_max_clips": 5,
    "allow_unreviewed_export": False,
    "min_clip_gap_seconds": 5,
    "export_subtitles": True,
    "export_concurrency": 4,
    "live_report_name": "拆条报告.md",
    "topic_review_enabled": True,
    "topic_review_provider": "stepfun_chat",
    "topic_review_model": "step-2-mini",
    "topic_review_timeout": 180,
    "topic_review_batch_size": 3,
    "topic_review_concurrency": 1,
    "topic_review_retry_attempts": 3,
    "topic_review_retry_backoff_seconds": 2.0,
    "topic_review_temperature": 0.2,
    "topic_review_reasoning_effort": "",
    "topic_review_api_key_env": "STEPFUN_API_KEY",
    "topic_review_base_url_env": "STEPFUN_BASE_URL",
    "topic_review_base_url": "https://api.stepfun.com/step_plan/v1",
    "topic_review_publish_ready_threshold": 80,
    "asr_provider": "stepaudio",
    "asr_model": "stepaudio-2.5-asr",
    "asr_timeout": 120,
    "asr_language": "zh",
    "asr_max_upload_bytes": 10 * 1024 * 1024,
    "asr_shard_seconds": 600,
    "asr_audio_sample_rate": 16000,
    "asr_audio_channels": 1,
    "asr_audio_format": "wav",
    "asr_retry_attempts": 3,
    "asr_retry_backoff_seconds": 1.0,
    "stepfun_api_key_env": "STEPFUN_API_KEY",
    "stepfun_base_url_env": "STEPFUN_BASE_URL",
    "stepfun_base_url": "https://api.stepfun.com/v1",
    "whisper_model": "small",
    "whisper_language": "zh",
    "whisper_timeout": 120,
    "whisper_output_format": "txt",
    "whisper_sample_rate": 16000,
    "whisper_channels": 1,
    "burn_subtitles": True,
    "filler_words": ["嗯", "啊", "呃", "哦", "唉", "呐", "嘛", "咯", "呀", "哎", "欸", "噢", "唔"],
    "subtitle_max_chars_per_line": 15,
    "subtitle_max_lines": 1,
    "subtitle_font": "Noto Sans CJK SC",
    "subtitle_font_size": 18,
    "subtitle_outline": 2,
    "subtitle_margin_v": 20,
}


def load_config_file(path, base_config=None):
    """读取并校验 JSON 配置文件，返回覆盖项。"""
    base_config = base_config or CONFIG
    try:
        with open(path, "r", encoding="utf-8") as config_file:
            payload = json.load(config_file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件 {path} 必须是合法 JSON：{exc.msg}") from exc
    except OSError as exc:
        raise ValueError(f"无法读取配置文件 {path}：{exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"配置文件 {path} 必须是 JSON object")

    for key, value in payload.items():
        if key not in base_config:
            raise ValueError(f"配置文件 {path} 包含未知配置项：{key}")
        try:
            _validate_config_value(key, value, base_config[key])
        except ValueError as exc:
            raise ValueError(f"配置文件 {path}：{exc}") from exc
    return payload


def merge_config_file(base_config, path):
    """把配置文件覆盖到 base_config 副本上。"""
    merged = dict(base_config)
    merged.update(load_config_file(path, base_config=base_config))
    return merged


def _validate_config_value(key, value, default_value):
    if value is None:
        raise ValueError(f"配置项 {key} 不允许为 null")

    expected_type = type(default_value)
    if expected_type is bool:
        if type(value) is not bool:
            raise ValueError(f"配置项 {key} 必须是 boolean")
        return
    if expected_type is int:
        if type(value) is not int:
            raise ValueError(f"配置项 {key} 必须是 integer")
        return
    if expected_type is float:
        if type(value) not in {int, float}:
            raise ValueError(f"配置项 {key} 必须是 number")
        return
    if expected_type is str:
        if type(value) is not str:
            raise ValueError(f"配置项 {key} 必须是 string")
        return

    if not isinstance(value, expected_type):
        raise ValueError(f"配置项 {key} 类型不匹配")
