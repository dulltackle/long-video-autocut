import importlib.util
import shutil
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT_DIR / "scripts" / "validate_documentation.py"


def load_validator():
    spec = importlib.util.spec_from_file_location("validate_documentation", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def copy_contract_documents(tmp_path):
    shutil.copytree(ROOT_DIR / "docs", tmp_path / "docs")
    shutil.copy2(ROOT_DIR / "CONTEXT.md", tmp_path / "CONTEXT.md")
    return tmp_path


def replace_required(path, old, new):
    original = path.read_text(encoding="utf-8")
    start = original.find(old)
    assert start >= 0, f"测试夹具已漂移，找不到待替换文本：{old[:80]}"
    line_number = original.count("\n", 0, start) + 1
    updated = original[:start] + new + original[start + len(old) :]
    path.write_text(updated, encoding="utf-8")
    return line_number


def find_line(path, needle):
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if needle in line:
            return line_number
    raise AssertionError(f"测试夹具已漂移，找不到目标行：{needle}")


def test_repository_documentation_contract_is_consistent():
    validator = load_validator()

    diagnostics = validator.validate_repository(ROOT_DIR)

    assert diagnostics == [], "\n".join(str(item) for item in diagnostics)


def test_validator_rejects_wrong_legacy_adr_status(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = root / "docs" / "adr" / "0002-export-all-publish-ready-live-clips-by-default.md"
    changed_line = replace_required(adr_path, "Status: Accepted", "Status: Amended")

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0002-export-all-publish-ready-live-clips-by-default.md")
        and item.line == changed_line
        and "旧 ADR 状态应为 Accepted，实际为 Amended" in item.message
        for item in diagnostics
    )


def test_validator_rejects_non_accepted_production_adr(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = (
        root
        / "docs"
        / "adr"
        / "0014-certify-linux-native-production-environment-and-reproducible-installation.md"
    )
    changed_line = replace_required(adr_path, "Status: Accepted", "Status: Superseded")

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path
        == Path(
            "docs/adr/0014-certify-linux-native-production-environment-and-reproducible-installation.md"
        )
        and item.line == changed_line
        and "新生产级 ADR 状态应为 Accepted，实际为 Superseded" in item.message
        for item in diagnostics
    )


def test_validator_rejects_superseded_adr_without_target(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = root / "docs" / "adr" / "0001-use-hybrid-topic-detection-for-live-clips.md"
    status_line = find_line(adr_path, "Status: Superseded")
    replace_required(
        adr_path,
        "Superseded by: [采用共享文本模型端口与分层主题评审]"
        "(0019-adopt-shared-text-model-port-and-layered-topic-review.md)\n",
        "",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0001-use-hybrid-topic-detection-for-live-clips.md")
        and item.line == status_line
        and "Superseded ADR 缺少有效的 Superseded by 目标" in item.message
        for item in diagnostics
    )


def test_validator_rejects_unreachable_adr_link(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = root / "docs" / "adr" / "0001-use-hybrid-topic-detection-for-live-clips.md"
    changed_line = replace_required(
        adr_path,
        "0019-adopt-shared-text-model-port-and-layered-topic-review.md",
        "0099-missing-production-decision.md",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0001-use-hybrid-topic-detection-for-live-clips.md")
        and item.line == changed_line
        and "本地链接目标不存在：0099-missing-production-decision.md" in item.message
        for item in diagnostics
    )


def test_validator_rejects_asymmetric_supersedes_relationship(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = (
        root / "docs" / "adr" / "0019-adopt-shared-text-model-port-and-layered-topic-review.md"
    )
    reverse_path = root / "docs" / "adr" / "0001-use-hybrid-topic-detection-for-live-clips.md"
    reverse_line = find_line(reverse_path, "Superseded by:")
    replace_required(
        adr_path,
        "Supersedes: [使用混合策略判定直播课主题片段]"
        "(0001-use-hybrid-topic-detection-for-live-clips.md)\n",
        "",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0001-use-hybrid-topic-detection-for-live-clips.md")
        and item.line == reverse_line
        and "关系不对称" in item.message
        and "Superseded by" in item.message
        and "0019-adopt-shared-text-model-port-and-layered-topic-review.md" in item.message
        for item in diagnostics
    )


def test_validator_rejects_relation_link_with_wrong_target_title(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = root / "docs" / "adr" / "0001-use-hybrid-topic-detection-for-live-clips.md"
    changed_line = replace_required(
        adr_path,
        "[采用共享文本模型端口与分层主题评审]",
        "[错误的生产决策标题]",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0001-use-hybrid-topic-detection-for-live-clips.md")
        and item.line == changed_line
        and "关系链接标题“错误的生产决策标题”" in item.message
        and "目标标题“采用共享文本模型端口与分层主题评审”" in item.message
        for item in diagnostics
    )


def test_validator_rejects_unreachable_production_spec_reference(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    spec_path = root / "docs" / "production-readiness-spec.md"
    changed_line = replace_required(
        spec_path,
        "adr/0002-export-all-publish-ready-live-clips-by-default.md",
        "adr/0099-missing-decision.md",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/production-readiness-spec.md")
        and item.line == changed_line
        and "本地链接目标不存在：adr/0099-missing-decision.md" in item.message
        for item in diagnostics
    )


def test_validator_rejects_unreachable_local_anchor(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = (
        root
        / "docs"
        / "adr"
        / "0014-certify-linux-native-production-environment-and-reproducible-installation.md"
    )
    changed_line = replace_required(
        adr_path,
        "#4-生产环境与安装",
        "#missing-production-section",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path
        == Path(
            "docs/adr/0014-certify-linux-native-production-environment-and-reproducible-installation.md"
        )
        and item.line == changed_line
        and "本地链接锚点不存在：#missing-production-section" in item.message
        for item in diagnostics
    )


def test_validator_rejects_production_spec_reference_to_wrong_adr(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    spec_path = root / "docs" / "production-readiness-spec.md"
    changed_line = replace_required(
        spec_path,
        "adr/0002-export-all-publish-ready-live-clips-by-default.md",
        "adr/0006-review-topic-candidates-in-neighboring-batches.md",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/production-readiness-spec.md")
        and item.line == changed_line
        and "内部 ADR 引用标题“默认导出全部发布就绪短视频”" in item.message
        and "目标标题“按相邻候选批次进行主题评审”" in item.message
        for item in diagnostics
    )


def test_validator_detects_stale_unreviewed_export_vocabulary(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    context_path = root / "CONTEXT.md"
    changed_line = replace_required(
        context_path,
        "根据完整主题评审结果和业务约束确定最终导出的短视频集合。"
        "默认选择全部发布就绪短视频；显式配置的数量上限只限制集合大小，"
        "不降低发布就绪标准。",
        "根据主题评审结果和业务约束确定最终导出的短视频集合。"
        "未评审、评审失败或缺少 API Key 时默认不导出短视频，"
        "只有用户显式允许未评审导出时才走兼容路径。",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("CONTEXT.md")
        and item.line == changed_line
        and "过期领域语义[未评审导出]" in item.message
        and "允许未评审导出" in item.message
        for item in diagnostics
    )


def test_validator_detects_stale_subtitle_degradation_vocabulary(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    context_path = root / "CONTEXT.md"
    changed_line = replace_required(
        context_path,
        "调用或校验失败会终止整次直播拆条运行。",
        "字幕优化失败时仍导出短视频并旁挂规则字幕，交由人工复核。",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("CONTEXT.md")
        and item.line == changed_line
        and "过期领域语义[字幕降级]" in item.message
        and "旁挂规则字幕" in item.message
        for item in diagnostics
    )


def test_validator_detects_stale_processing_cache_scope(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    context_path = root / "CONTEXT.md"
    changed_line = replace_required(
        context_path,
        "直播拆条过程中可复用的完整、已校验中间结果，分为整场转写、识别分片、"
        "主题评审和字幕优化四个独立命名空间。处理缓存使用版本化内容身份区分"
        "所有影响结果的输入变化，不保存部分或失败结果。",
        "直播拆条过程中复用的中间结果，包括识别分片转写、整场转写文本和"
        "字幕优化结果；后续可扩展到主题评审结果。",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("CONTEXT.md")
        and item.line == changed_line
        and "过期领域语义[旧处理缓存范围]" in item.message
        and "后续可扩展到主题评审" in item.message
        for item in diagnostics
    )


def test_validator_detects_stale_run_diagnostics_semantics(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    context_path = root / "CONTEXT.md"
    changed_line = replace_required(
        context_path,
        "按运行标识归档在 `work/runs/<run_id>/` 的脱敏运行证据",
        "按运行标识归档在 `runs/<run_id>/` 的脱敏运行证据",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("CONTEXT.md")
        and item.line == changed_line
        and "过期领域语义[旧运行诊断语义]" in item.message
        and "runs/<run_id>/" in item.message
        for item in diagnostics
    )


def test_cli_reports_all_diagnostics_and_returns_failure(tmp_path, capsys):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = root / "docs" / "adr" / "0002-export-all-publish-ready-live-clips-by-default.md"
    status_line = replace_required(adr_path, "Status: Accepted", "Status: Amended")
    context_path = root / "CONTEXT.md"
    context_line = replace_required(context_path, "`work/runs/<run_id>/`", "`runs/<run_id>/`")

    exit_code = validator.main([str(root)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert (
        f"docs/adr/0002-export-all-publish-ready-live-clips-by-default.md:{status_line}: "
        "旧 ADR 状态应为 Accepted，实际为 Amended"
    ) in captured.err
    assert f"CONTEXT.md:{context_line}: 过期领域语义[旧运行诊断语义]" in captured.err


def test_validator_rejects_self_as_superseded_replacement_target(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = root / "docs" / "adr" / "0001-use-hybrid-topic-detection-for-live-clips.md"
    changed_line = replace_required(
        adr_path,
        "Superseded by: [采用共享文本模型端口与分层主题评审]"
        "(0019-adopt-shared-text-model-port-and-layered-topic-review.md)",
        "Superseded by: [使用混合策略判定直播课主题片段]"
        "(0001-use-hybrid-topic-detection-for-live-clips.md)\n"
        "Supersedes: [使用混合策略判定直播课主题片段]"
        "(0001-use-hybrid-topic-detection-for-live-clips.md)",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0001-use-hybrid-topic-detection-for-live-clips.md")
        and item.line == changed_line
        and "Superseded by 目标必须是不同的 Accepted ADR" in item.message
        for item in diagnostics
    )


def test_validator_rejects_ambiguous_duplicate_status_metadata(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = root / "docs" / "adr" / "0002-export-all-publish-ready-live-clips-by-default.md"
    changed_line = replace_required(
        adr_path,
        "Status: Accepted\n",
        "Status: Accepted\nStatus: Superseded\n",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0002-export-all-publish-ready-live-clips-by-default.md")
        and item.line == changed_line
        and "Status 元数据必须恰好一条，实际为 2 条" in item.message
        for item in diagnostics
    )


def test_validator_reports_missing_production_spec_instead_of_crashing(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    (root / "docs" / "production-readiness-spec.md").unlink()

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/production-readiness-spec.md")
        and item.line == 1
        and "缺少生产就绪规格" in item.message
        for item in diagnostics
    )


def test_validator_reports_missing_domain_context_instead_of_crashing(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    (root / "CONTEXT.md").unlink()

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("CONTEXT.md")
        and item.line == 1
        and "缺少领域文档" in item.message
        for item in diagnostics
    )


def test_validator_rejects_external_superseded_replacement_target(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    old_adr_path = root / "docs" / "adr" / "0001-use-hybrid-topic-detection-for-live-clips.md"
    new_adr_path = (
        root / "docs" / "adr" / "0019-adopt-shared-text-model-port-and-layered-topic-review.md"
    )
    status_line = find_line(old_adr_path, "Status: Superseded")
    replace_required(
        old_adr_path,
        "[采用共享文本模型端口与分层主题评审]"
        "(0019-adopt-shared-text-model-port-and-layered-topic-review.md)",
        "[外部替代说明](https://example.com/replacement)",
    )
    replace_required(
        new_adr_path,
        "Supersedes: [使用混合策略判定直播课主题片段]"
        "(0001-use-hybrid-topic-detection-for-live-clips.md)\n",
        "",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0001-use-hybrid-topic-detection-for-live-clips.md")
        and item.line == status_line
        and "Superseded ADR 缺少有效的 Superseded by 目标" in item.message
        for item in diagnostics
    )


def test_validator_rejects_amended_adr_without_target(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    old_adr_path = (
        root / "docs" / "adr" / "0005-use-stepfun-chat-as-default-topic-review-model.md"
    )
    new_adr_path = (
        root / "docs" / "adr" / "0019-adopt-shared-text-model-port-and-layered-topic-review.md"
    )
    status_line = find_line(old_adr_path, "Status: Amended")
    replace_required(
        old_adr_path,
        "Amended by: [采用共享文本模型端口与分层主题评审]"
        "(0019-adopt-shared-text-model-port-and-layered-topic-review.md)\n",
        "",
    )
    replace_required(
        new_adr_path,
        "Amends: [默认使用 StepFun Chat 进行主题评审]"
        "(0005-use-stepfun-chat-as-default-topic-review-model.md)\n",
        "",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0005-use-stepfun-chat-as-default-topic-review-model.md")
        and item.line == status_line
        and "Amended ADR 缺少有效的 Amended by 目标" in item.message
        for item in diagnostics
    )


def test_validator_ignores_status_examples_outside_metadata(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = root / "docs" / "adr" / "0002-export-all-publish-ready-live-clips-by-default.md"
    replace_required(adr_path, "Status: Accepted\n", "")
    adr_path.write_text(
        adr_path.read_text(encoding="utf-8")
        + "\n```markdown\nStatus: Accepted\n```\n",
        encoding="utf-8",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0002-export-all-publish-ready-live-clips-by-default.md")
        and "旧 ADR 缺少 Status 元数据" in item.message
        for item in diagnostics
    )


def test_validator_rejects_unreachable_reference_style_link(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    spec_path = root / "docs" / "production-readiness-spec.md"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8")
        + "\n[缺失的内部引用][missing-ref]\n\n"
        "[missing-ref]: adr/0099-missing-decision.md\n",
        encoding="utf-8",
    )
    reference_line = find_line(spec_path, "[缺失的内部引用][missing-ref]")

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/production-readiness-spec.md")
        and item.line == reference_line
        and "本地链接目标不存在：adr/0099-missing-decision.md" in item.message
        for item in diagnostics
    )


def test_validator_ignores_link_examples_inside_inline_code(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    spec_path = root / "docs" / "production-readiness-spec.md"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8")
        + "\n示例：`[不是实际链接](adr/0099-missing-decision.md)`。\n",
        encoding="utf-8",
    )

    diagnostics = validator.validate_repository(root)

    assert not any(
        item.path == Path("docs/production-readiness-spec.md")
        and "adr/0099-missing-decision.md" in item.message
        for item in diagnostics
    )


def test_validator_reports_malformed_url_instead_of_crashing(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    spec_path = root / "docs" / "production-readiness-spec.md"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8") + "\n[格式错误的链接](http://[invalid)\n",
        encoding="utf-8",
    )
    bad_link_line = find_line(spec_path, "http://[invalid")

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/production-readiness-spec.md")
        and item.line == bad_link_line
        and "链接目标格式非法：http://[invalid" in item.message
        for item in diagnostics
    )


def test_vocabulary_scan_allows_explicit_rejection_of_unreviewed_export(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    context_path = root / "CONTEXT.md"
    safe_sentence = "当前生产契约不允许未评审导出。"
    anchor = "**导出选择**:\n"
    replace_required(context_path, anchor, anchor + safe_sentence + "\n")
    safe_line = find_line(context_path, safe_sentence)

    diagnostics = validator.validate_repository(root)

    assert not any(
        item.path == Path("CONTEXT.md")
        and item.line == safe_line
        and "过期领域语义[未评审导出]" in item.message
        for item in diagnostics
    )


def test_vocabulary_scan_detects_sidecar_srt_subtitle_fallback(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    context_path = root / "CONTEXT.md"
    stale_sentence = "字幕优化失败时旁挂规则 SRT 并交由人工复核。"
    anchor = "**字幕优化**:\n"
    changed_line = replace_required(context_path, anchor, anchor + stale_sentence + "\n")
    stale_line = changed_line + 1

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("CONTEXT.md")
        and item.line == stale_line
        and "过期领域语义[字幕降级]" in item.message
        and "旁挂规则 SRT" in item.message
        for item in diagnostics
    )


def test_validator_rejects_symmetric_but_unapproved_replacement_rewiring(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    old_adr_path = root / "docs" / "adr" / "0001-use-hybrid-topic-detection-for-live-clips.md"
    original_new_path = (
        root / "docs" / "adr" / "0019-adopt-shared-text-model-port-and-layered-topic-review.md"
    )
    wrong_new_path = (
        root
        / "docs"
        / "adr"
        / "0018-adopt-provider-agnostic-speech-recognition-and-coverage-ledger.md"
    )
    replace_required(
        old_adr_path,
        "[采用共享文本模型端口与分层主题评审]"
        "(0019-adopt-shared-text-model-port-and-layered-topic-review.md)",
        "[采用供应商无感知的语音识别模块与覆盖账本]"
        "(0018-adopt-provider-agnostic-speech-recognition-and-coverage-ledger.md)",
    )
    replace_required(
        original_new_path,
        "Supersedes: [使用混合策略判定直播课主题片段]"
        "(0001-use-hybrid-topic-detection-for-live-clips.md)\n",
        "",
    )
    replace_required(
        wrong_new_path,
        "[分片重叠 + 覆盖度兜底补转修复 ASR 尾部丢失]"
        "(0013-overlap-asr-shards-and-backfill-tail-coverage.md)",
        "[分片重叠 + 覆盖度兜底补转修复 ASR 尾部丢失]"
        "(0013-overlap-asr-shards-and-backfill-tail-coverage.md), "
        "[使用混合策略判定直播课主题片段]"
        "(0001-use-hybrid-topic-detection-for-live-clips.md)",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0001-use-hybrid-topic-detection-for-live-clips.md")
        and "决策图缺少批准的 Superseded by 关系" in item.message
        and "0019-adopt-shared-text-model-port-and-layered-topic-review.md" in item.message
        for item in diagnostics
    )


def test_validator_rejects_extra_external_adr_relationship(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = (
        root / "docs" / "adr" / "0019-adopt-shared-text-model-port-and-layered-topic-review.md"
    )
    changed_line = replace_required(
        adr_path,
        "Supersedes: [使用混合策略判定直播课主题片段]"
        "(0001-use-hybrid-topic-detection-for-live-clips.md)",
        "Supersedes: [使用混合策略判定直播课主题片段]"
        "(0001-use-hybrid-topic-detection-for-live-clips.md), "
        "[外部关系](https://example.com/other-decision)",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path
        == Path("docs/adr/0019-adopt-shared-text-model-port-and-layered-topic-review.md")
        and item.line == changed_line
        and "ADR 关系目标必须是 docs/adr 内的本地文件" in item.message
        and "https://example.com/other-decision" in item.message
        for item in diagnostics
    )


def test_validator_rejects_partially_malformed_relationship_list(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = (
        root / "docs" / "adr" / "0019-adopt-shared-text-model-port-and-layered-topic-review.md"
    )
    changed_line = replace_required(
        adr_path,
        "Supersedes: [使用混合策略判定直播课主题片段]"
        "(0001-use-hybrid-topic-detection-for-live-clips.md)",
        "Supersedes: [使用混合策略判定直播课主题片段]"
        "(0001-use-hybrid-topic-detection-for-live-clips.md), "
        "[损坏关系](0003-use-stepaudio-as-default-transcription-provider.md",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path
        == Path("docs/adr/0019-adopt-shared-text-model-port-and-layered-topic-review.md")
        and item.line == changed_line
        and "ADR 关系元数据格式非法" in item.message
        for item in diagnostics
    )


def test_validator_rejects_unquoted_relationship_link_title(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = root / "docs" / "adr" / "0001-use-hybrid-topic-detection-for-live-clips.md"
    changed_line = replace_required(
        adr_path,
        "(0019-adopt-shared-text-model-port-and-layered-topic-review.md)",
        "(0019-adopt-shared-text-model-port-and-layered-topic-review.md arbitrary-junk)",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0001-use-hybrid-topic-detection-for-live-clips.md")
        and item.line == changed_line
        and "ADR 关系元数据格式非法" in item.message
        for item in diagnostics
    )


def test_validator_ignores_fenced_metadata_before_real_title(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    adr_path = root / "docs" / "adr" / "0002-export-all-publish-ready-live-clips-by-default.md"
    replace_required(adr_path, "Status: Accepted\n", "")
    adr_path.write_text(
        "```markdown\n"
        "# 默认导出全部发布就绪短视频\n"
        "Status: Accepted\n"
        "```\n\n"
        + adr_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/adr/0002-export-all-publish-ready-live-clips-by-default.md")
        and "旧 ADR 缺少 Status 元数据" in item.message
        for item in diagnostics
    )


def test_validator_rejects_unreachable_shortcut_reference_link(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    spec_path = root / "docs" / "production-readiness-spec.md"
    shortcut = "[缺失的 shortcut 引用]"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8")
        + f"\n{shortcut}\n\n{shortcut}: adr/0099-missing-decision.md\n",
        encoding="utf-8",
    )
    reference_line = find_line(spec_path, shortcut)

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("docs/production-readiness-spec.md")
        and item.line == reference_line
        and "本地链接目标不存在：adr/0099-missing-decision.md" in item.message
        for item in diagnostics
    )


def test_vocabulary_scan_allows_explicit_retirement_of_unreviewed_export(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    context_path = root / "CONTEXT.md"
    safe_sentence = "当前生产契约不再允许未评审导出。"
    anchor = "**导出选择**:\n"
    replace_required(context_path, anchor, anchor + safe_sentence + "\n")
    safe_line = find_line(context_path, safe_sentence)

    diagnostics = validator.validate_repository(root)

    assert not any(
        item.path == Path("CONTEXT.md")
        and item.line == safe_line
        and "过期领域语义[未评审导出]" in item.message
        for item in diagnostics
    )


def test_vocabulary_scan_detects_sidecar_srt_after_subtitle_failure(tmp_path):
    validator = load_validator()
    root = copy_contract_documents(tmp_path)
    context_path = root / "CONTEXT.md"
    stale_sentence = "字幕优化失败时旁挂 SRT，交由人工复核。"
    anchor = "**字幕优化**:\n"
    changed_line = replace_required(context_path, anchor, anchor + stale_sentence + "\n")
    stale_line = changed_line + 1

    diagnostics = validator.validate_repository(root)

    assert any(
        item.path == Path("CONTEXT.md")
        and item.line == stale_line
        and "过期领域语义[字幕降级]" in item.message
        and "旁挂 SRT" in item.message
        for item in diagnostics
    )
