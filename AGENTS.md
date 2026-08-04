# Agent instructions

## Agent skills

### Issue tracker

本仓库使用 GitHub Issues 跟踪议题与规格。详见 `docs/agents/issue-tracker.md`。

### Triage labels

本仓库使用默认的五类 triage 标签。详见 `docs/agents/triage-labels.md`。

### Domain docs

本仓库采用单上下文领域文档布局：根级 `CONTEXT.md` 与 `docs/adr/`。详见 `docs/agents/domain.md`。

## 测试与契约迁移

- 测试失败时，默认只修复生产代码，使其满足现行契约。
- 只有已批准决定明确废止旧契约，并且替代契约测试已经建立时，才允许在同一次
  契约迁移中删除或重写与新契约冲突的旧测试。
- 即使进行已批准的契约迁移，也不得通过放宽断言、跳过测试、篡改 Mock、
  Fixture 或测试辅助逻辑，或者增加测试级重试来掩盖生产缺陷。
- 与已批准契约迁移无关的失败，不得修改测试相关代码规避。

## 生产发布与门禁验证

生产版本批准遵循 ADR-0025 分层验收证据契约。发布门禁使用同一 CI 构建物 release-bundle.tar 包内的 6 个核心发布工具：
- `scripts/install-production.sh`：认证主机可复现生产安装脚本
- `scripts/run_keyless_gate_network.sh`：无密钥网络门禁验证
- `scripts/run_release_gate.py`：发布门禁执行器
- `scripts/systemd_credential_bridge.py`：凭据安全桥接
- `scripts/validate_installed_delivery.py`：已安装交付物验证
- `scripts/validate_release_evidence.py`：发布证据完整性校验

发布测试与验证必须使用 `umask 0022` 权限掩码及认证 Ubuntu 24.04 LTS 环境。

