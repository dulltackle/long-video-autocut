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
