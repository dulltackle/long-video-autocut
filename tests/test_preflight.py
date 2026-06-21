from video_auto_editor.config import CONFIG
from video_auto_editor.preflight import (
    EnvironmentProbe,
    run_preflight,
)


def make_probe(commands=None, env=None):
    default_commands = {
        "video-auto-editor": True,
        "ffmpeg": True,
        "ffprobe": True,
    }
    default_commands.update(commands or {})
    default_env = {"STEPFUN_API_KEY": "sk-test"}
    if env is not None:
        default_env = env
    return EnvironmentProbe(commands=default_commands, env=default_env)


def find(result, name):
    return next(check for check in result.checks if check.name == name)


def test_all_dependencies_ready():
    result = run_preflight(make_probe())

    assert result.ready is True
    assert result.errors == []
    assert all(check.status in {"ok", "warn"} for check in result.checks)


def test_missing_stepfun_key_is_error_with_hint():
    result = run_preflight(make_probe(env={}))

    check = find(result, "STEPFUN_API_KEY")
    assert check.status == "error"
    assert "STEPFUN_API_KEY" in check.hint
    assert result.ready is False


def test_missing_ffmpeg_is_error():
    result = run_preflight(make_probe(commands={"ffmpeg": False}))

    assert find(result, "ffmpeg").status == "error"
    assert result.ready is False


def test_missing_ffprobe_is_error():
    result = run_preflight(make_probe(commands={"ffprobe": False}))

    assert find(result, "ffprobe").status == "error"
    assert result.ready is False


def test_missing_cli_is_error():
    result = run_preflight(make_probe(commands={"video-auto-editor": False}))

    assert find(result, "video-auto-editor").status == "error"
    assert result.ready is False


def test_review_disabled_is_warn_not_error():
    config = CONFIG.copy()
    config["topic_review_enabled"] = False

    result = run_preflight(make_probe(), config=config)

    review = find(result, "topic_review")
    assert review.status == "warn"
    assert "未评审降级" in review.detail
    assert result.ready is True
    assert result.warnings


def test_review_enabled_without_key_is_warn():
    result = run_preflight(make_probe(env={}))

    # 缺 key 时 STEPFUN_API_KEY 报 error，但评审项本身只是降级 warn。
    assert find(result, "topic_review").status == "warn"


def test_whisper_provider_is_warn_without_key():
    config = CONFIG.copy()
    config["asr_provider"] = "whisper"

    result = run_preflight(make_probe(env={}), config=config)

    assert find(result, "asr_provider").status == "warn"


def test_default_probe_runs_without_external_commands():
    # 使用真实探测接口也不应抛错（结果可能 not ready，但流程稳定）。
    result = run_preflight()
    assert isinstance(result.ready, bool)
    assert result.checks
