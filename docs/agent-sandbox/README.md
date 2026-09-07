# Agent 数据操作沙盒 · 落地交付物

本目录是对根目录 [`README.md`](../../README.md)（《Agent 数据操作沙盒》评审报告第二版）的落地实现：
把报告里的判断与方法论建议，变成**可执行、可校验、可复核**的交付物。

## 阅读顺序

| 想做什么 | 看哪个 |
| --- | --- |
| 马上要开评审会 | [09 开放问题确认清单](09-open-questions.md) → [10 工作量估算](10-effort-estimation.md) |
| 要启动技术预研 | [02 A/B spike 方案](02-spike-ab-plan.md) → [01 ChangeSet 模型](01-changeset-model.md) |
| 要验证需求是否成立 | [13 客户访谈提纲](13-customer-interview-guide.md) |
| 要开始写代码 | [01 ChangeSet](01-changeset-model.md) + [03 API/MCP 契约](03-api-and-mcp-contract.md) + [04 状态机](04-sandbox-lifecycle.md) |
| 要准备 POC | [08 POC 验收](08-poc-acceptance.md) + [07 基准测试](07-benchmark-suite.md) |
| 要对外表述 / 应对竞品提问 | [12 竞品情报台账](12-competitor-intel.md) |

## 交付物清单

### 技术设计

| # | 文档 | 攻关的难点 | 配套可执行件 |
| --- | --- | --- | --- |
| 01 | [ChangeSet 数据模型与差异摘要格式](01-changeset-model.md) | 难点 1 差异语义还原 | [`schema/changeset.schema.json`](schema/changeset.schema.json)、3 份示例、[`tools/validate_changeset.py`](tools/validate_changeset.py) |
| 02 | [A/B 路径对照 spike 实验方案](02-spike-ab-plan.md) | 难点 1 | [`spike/fixtures/workload.mysql.sql`](spike/fixtures/workload.mysql.sql)、[`spike/compare_changesets.py`](spike/compare_changesets.py) |
| 03 | [REST API 与 MCP 工具契约](03-api-and-mcp-contract.md) | 难点 4 双路径 SLA、难点 5 Agent 风险面 | [`schema/openapi.yaml`](schema/openapi.yaml)、[`schema/mcp-tools.json`](schema/mcp-tools.json) |
| 04 | [沙盒状态机与生命周期](04-sandbox-lifecycle.md) | 难点 3 并发与资源泄漏 | [`schema/sandbox-state-machine.json`](schema/sandbox-state-machine.json)、[`tools/validate_state_machine.py`](tools/validate_state_machine.py) |
| 05 | [审批工作流与权限模型](05-approval-and-permissions.md) | 难点 2 合并降维、难点 5 安全合规 | 门禁规则由 `validate_changeset.py` 实现 |
| 06 | [Airflow DAG 与 Celery 队列/配额方案](06-orchestration.md) | 难点 3 | —— |
| 07 | [基准测试套件设计](07-benchmark-suite.md) | 报告 18.6 | —— |
| 08 | [POC 验收指标与自动化校验](08-poc-acceptance.md) | 报告 18.5 | [`tools/poc_gate.py`](tools/poc_gate.py)、[`templates/poc-metrics.template.json`](templates/poc-metrics.template.json) |

### 管理与方法论

| # | 文档 | 落实报告的哪一节 |
| --- | --- | --- |
| 09 | [开放问题确认清单](09-open-questions.md) | 第十五部分（8 个开放问题） |
| 10 | [工作量估算表（L1/L2/L3 + 三点估计）](10-effort-estimation.md) | 18.3 |
| 11 | [风险台账](11-risk-register.md) | 18.7 |
| 12 | [竞品情报台账](12-competitor-intel.md) | 18.1 / 18.2 |
| 13 | [客户访谈提纲与验收判据](13-customer-interview-guide.md) | 18.4 |

## 一键校验

所有可执行件都带自测，无需数据库环境即可运行（只依赖 Python 3.9+，`jsonschema` 可选）：

```bash
python3 docs/agent-sandbox/tools/check_all.py
```

它会依次运行 ChangeSet 门禁校验、A/B 比对器、状态机不变式校验、POC 判定器的全部自测。

## 三条贯穿全部文档的硬规则

1. **源端零改动**：任何设计不得在源端新增挂载、权限或代理（POC 红线指标 = 0）；
2. **差异不可信就不放行**：`integrity.completeness != complete` 的变更集禁止进入审批与合并；
3. **Agent 不可自审自批**：MCP 层不暴露审批与合并执行能力，能力边界在服务端而非提示词里。

## 当前状态与前置条件

这些是**设计稿**，尚未经一牧内部代码校验。进入阶段 1 之前必须先完成两件事
（对应根 README 第十六部分的「技术预研」与 18.4）：

- 技术侧：完成 02 文档的 A/B spike，把 E2、E6 从 L3 降为 L2；
- 需求侧：完成 13 文档的客户访谈，至少 3 家达成判据。

任一不通过，项目应维持在预研状态而非产品立项。
