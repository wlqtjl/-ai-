# Agent 数据操作沙盒 · 落地交付物

本目录是对根目录 [`README.md`](../../README.md)（《Agent 数据操作沙盒》评审报告第二版）的落地实现：
把报告里的判断与方法论建议，变成**可执行、可校验、可复核**的交付物。

> **本目录已经过第二轮批判性复核并整体修订。** 复核发现第一轮的交付物
> 「在工程完备性上做得过头，在前提正确性上做得不足」：产出了 schema、状态机、校验脚本这些
> 看起来很扎实的东西，却没有先质疑几个会让整个方案不成立的前提。三个致命矛盾是：
>
> 1. **脱敏默认开启 与 合并回生产互斥**（脱敏值写回生产 = 数据污染）→ 首版改为「变更导出」（**已定稿**）；
> 2. **块级差异与逻辑日志的语义类型不同**（净状态差 vs 操作序列）→ 规范形式统一为净状态差；
> 3. **数据通路带宽比目标场景低 1-2 个数量级**（实测 20 MB/s vs 需求 ~1 GB/s）→ 新增阶段 0 第三出口判据。
>
> 全部未显式记录的前提已收敛到 [00 假设清单](00-assumptions.md)，**请先读它**。

## 阅读顺序

| 想做什么 | 看哪个 |
| --- | --- |
| **第一次读 / 想知道哪些前提没被验证** | [00 假设清单](00-assumptions.md) |
| 想知道首版做什么 / 不做什么 | [05 文档 1.2](05-approval-and-permissions.md)（**首版 = 变更导出，已定稿**）|
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

另有 [00 假设清单](00-assumptions.md)：所有技术文档的公共前提，含「若不成立的后果」与「验证方式」。

### 管理与方法论

| # | 文档 | 落实报告的哪一节 |
| --- | --- | --- |
| 09 | [开放问题确认清单](09-open-questions.md) | 第十五部分（8 个开放问题） |
| 10 | [工作量估算表（L1/L2/L3 + 蒙特卡洛 P10/P50/P90）](10-effort-estimation.md) | 18.3（配套 [`tools/estimate_montecarlo.py`](tools/estimate_montecarlo.py)）|
| 11 | [风险台账](11-risk-register.md) | 18.7 |
| 12 | [竞品情报台账](12-competitor-intel.md) | 18.1 / 18.2 |
| 13 | [客户访谈提纲与验收判据](13-customer-interview-guide.md) | 18.4 |

## 一键校验（请先读这段说明）

> ⚠️ **这些脚本做的是「契约自洽性检查」，不构成对系统行为的任何验证。**
>
> 它们校验的是「本仓库的 JSON/YAML 文件是否符合本仓库自己写下的规则」。
> 第一轮用「4/4 PASS，全部自测通过」来描述它们，这句话给人的暗示远超它的实际保证——
> 那正是原报告批评的「用看起来有力的表述掩盖论证强度不足」，第一轮在批评它的同时重犯了一次。
>
> 更具体地说：`validate_state_machine.py` 的 INV-6 检查的是转换的 trigger 字符串里
> **有没有出现某几个字**——这是在检查标签，不是在检查行为。
>
> 真正的正确性证据只能来自 [02 文档](02-spike-ab-plan.md)的 spike 与真实环境实测。

所有可执行件都带自测，无需数据库环境即可运行（只依赖 Python 3.9+，`jsonschema` 可选）：

```bash
python3 docs/agent-sandbox/tools/check_all.py
```

它会依次运行 ChangeSet 契约检查、A/B 比对器、状态机不变式检查、POC 判定器、
以及工作量蒙特卡洛模拟的全部自测。

## 三条贯穿全部文档的硬规则

1. **源端零改动**：任何设计不得在源端新增挂载、权限或代理（POC 红线指标 = 0）；
2. **差异不可信就不放行**：`integrity.completeness != complete`、存在冲突、
   或违反净状态差不变式的变更集，禁止进入审批流程；
3. **Agent 不可自审自批**：MCP 层不暴露审批能力，能力边界在服务端而非提示词里；
4. **首版不写生产库**（第二轮新增）：审批通过后产出变更集导出物，
   由客户既有变更流程执行（[05 文档 1.2](05-approval-and-permissions.md)，**已于 2026-09-07 定稿**）；
5. **指标必须可证明**（第二轮新增）：写不出判定方式的指标不许进指标表；
   抽样结论只能表述为区间或上界，不得表述为「= 100%」。

## 当前状态与前置条件

这些是**设计稿**，尚未经一牧内部代码校验。进入阶段 1 之前必须完成**三件并行的事**
（第二轮由两件增至三件）：

| # | 内容 | 出口判据 |
| --- | --- | --- |
| 1 | 技术路线：02 文档的 A/B spike | fixture 全集零失配，E2 依据等级降为 L2 |
| 2 | 需求：13 文档的客户访谈 | 至少 3 家拿得出**行为证据**（预算/立项/工单/PoC），且含 1-2 家非现有客户对照 |
| 3 | **物理约束（新增）**：02 文档第 7 节数据通路实测 | 有效带宽与并发衰减曲线齐备；测不出目标量级则**先下调指标再谈开发** |

前置的产品决策（[09 #9](09-open-questions.md)）**已落定**：
首版为「**变更导出**」——沙盒可写、用完即焚、产出经审核的变更集，
由客户既有变更流程执行，**一牧的产品不写客户的生产库**。
脱敏随之升为不可关闭的合规红线，分支「不脱敏 + 直接合并」移出首版范围。

上表三条出口判据与该决策相互独立，仍须逐条达成；任一不通过，
项目应维持在预研状态而非产品立项。

## 诚实的自我评价

第一轮在两周的模拟工作量里产出了约 3000 行文档和代码，
**但真正决定项目成败的三个问题（脱敏与合并的矛盾、语义类型不匹配、带宽差两个数量级）一个都没碰到**。
产出体量和判断质量不是一回事——这恰恰是原报告第十八部分想说的事。
本轮修订就是对自己的交付物执行一次同样的方法论。
