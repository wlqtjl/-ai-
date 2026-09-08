# 01 · ChangeSet 数据模型与差异摘要格式

> ## ⚠️ 先读这段：这是设计稿，不是已有功能
>
> **一牧目前没有为「Agent 数据沙箱」写过任何一行代码。**
> 本目录（`docs/agent-sandbox/`）下的全部文档、schema、脚本，都是外部顾问产出的**设计草案**，
> 用途是供内部评审和排期讨论，**不代表一牧已经具备其中描述的任何能力**。
>
> 凡是本文里写「系统会……」「接口返回……」的地方，都应读作「**如果做，建议这样做**」。
> 对外沟通、商务材料、客户演示中，**不得**引用本目录内容作为已有能力的证明。
>
> 术语看不懂请查 [术语对照表（大白话）](GLOSSARY.md)；想快速了解全貌请看 [大白话总览](overview-plain.md)。

## 一句话说明（大白话）

**这份文档在定义「改动清单」长什么样。**

Agent 在沙箱里改完数据后，得有一份东西告诉人类「它到底改了什么」，比如：

> 订单表，订单号 20260907001 这行，
> 状态：从「已结算」改成「待结算」

一批这样的记录合起来就是「改动清单」（文档里叫 ChangeSet）。DBA 拿它审批，审批完拿去生产库执行。
**没有这份清单，沙箱就只是个能随便折腾的测试库，跟现有的测试环境没区别。**

有一条重要规则：**同一行被改了三次，清单上只写「最初 → 最终」一条，不写三条流水账。**
因为拿去生产库执行时，中间过程没有意义。

给谁看：研发（要照着实现）、DBA（要看得懂）。

---

> 对应计划「四、可立即产出 · 第 1 项」，攻关「难点 1：ChangeSet 差异语义还原」。
> 状态：设计稿（未经一牧内部代码校验）。
> **第二轮复核修订**：规范形式由「操作序列」改为「净状态差」，`position` 降级为溯源信息。修订理由见第 3 节。

## 0. 本文依赖的假设

| 假设 | 类型 | 若不成立 |
| --- | --- | --- |
| [B1](00-assumptions.md) 规范形式为净状态差即可满足下游需求 | 设计决策 | 若审批/审计需要操作序列，需另建表示 |
| [B2](00-assumptions.md) 逻辑日志可无损折叠为净状态差 | **假设** | A/B 两条路径不可比，spike 失去意义 |
| [B3](00-assumptions.md) 变更行可用主键稳定定位 | 事实（有边界） | 无主键表已显式拒绝 |
| [A5](00-assumptions.md) Agent 写入量远小于库体量 | 推断 | 逻辑日志开销论据失效 |

完整清单见 [00 假设清单](00-assumptions.md)。

## 1. 为什么先做这一层

ChangeSet 是整个方案里唯一的核心新增算法模块，同时是下游所有能力的公共依赖：

- 差异摘要 UI、审批工作流、变更导出/合并、审计留痕，全部消费同一个 ChangeSet 结构；
- A/B 两条采集路径必须产出**同一个语义类型**的结构，否则无法比对。

因此本文件先定义**语义契约**，再谈实现路径。采集路径是可替换的，数据模型不是。

## 2. 语义参照物（不自造语义）

| 参照物 | 借鉴点 | 本模型的取舍 |
| --- | --- | --- |
| Dolt 行级 diff/merge | `from`/`to` 双状态行表示、冲突标记 | 采纳双状态行表示；首版不做三方合并，冲突只标记不自动解决 |
| Delta Lake Change Data Feed | `_change_type` ∈ insert/update_preimage/update_postimage/delete | 合并为单条 `update` 记录并同时携带 before/after |
| Oracle LogMiner / MySQL binlog(ROW) | 行级前后镜像 | 只借鉴镜像表示；**位点与事务边界降级为溯源信息**，不作为语义主干 |

## 3. 规范形式：净状态差，而非操作序列（第二轮修订的核心）

### 3.1 第一轮的设计错误

第一轮同时规定了两件互斥的事：

- 「合并按 `position` 升序重放」——这是**操作序列**（sequence of operations）语义；
- 块级差异路径也要产出同一结构——但它物理上只能给出**净状态差**（net state diff），`position` 恒为 null。

两种语义类型对同一组操作**在定义上就不可能等价**。例如「同一行连续修改 3 次」，
操作序列是 3 条记录，净状态差是 1 条。第一轮却把「两条路径表示必须相同」写成了 spike 的通过判据——
这是一个**永远无法通过的判据**，等于把实验设计成了必然失败。

### 3.2 修订后的定义

**ChangeSet 的规范形式是净状态差**：描述「基线状态 → 当前状态」的差异，而不是「发生过哪些操作」。

理由：

1. 下游真正需要的就是净差。无论是导出变更脚本还是合并回生产，需要的是「最终要把数据变成什么样」，
   而不是「中间经历了什么」；
2. 只有净差才是两条采集路径的**公共可表达形式**，spike 才具备可比性；
3. 净差天然消除了「插入后又删除」「回滚的事务」这类噪声。

对两条路径的要求：

| 路径 | 要求 |
| --- | --- |
| 逻辑日志 | 采集后**必须折叠为净差**再输出：同行多次修改合并为一条、净效果为零的变更剔除、未提交事务剔除 |
| 块级差异 | 天然是净差，直接映射 |

`position` / `txn_id` / `occurred_at` 降级为**可选的溯源信息**（供审计追溯用），
**不参与**合并排序，也**不参与** A/B 比对。

### 3.3 折叠规则（逻辑日志路径必须实现）

| 原始序列 | 折叠结果 |
| --- | --- |
| insert → update | insert（after 取最终值） |
| insert → delete | **无记录**（净效果为零） |
| update → update | 单条 update（before 取最初值，after 取最终值） |
| update → delete | delete（before 取最初值） |
| delete → insert | update（before 取删除前值，after 取插入值）；主键相同才成立 |
| 未提交/已回滚事务 | **无记录** |

折叠后若 `before == after`，该行**不得**出现在 ChangeSet 中。

## 4. 采集路径分层策略

| 层级 | 采集方式 | 首版定位 |
| --- | --- | --- |
| L-A（首选） | 沙盒库自身逻辑变更能力：Oracle LogMiner、MySQL binlog(ROW)、PG 逻辑复制槽 | **首版唯一路径** |
| L-B | 块级快照差异 → 反查数据文件结构还原表/行 | **首版范围外**，理由见下 |
| L-C（校验） | 两条路径同时跑，比对净差 | 仅用于预研 spike 与回归测试 |

**L-B 首版范围外的理由**（第二轮修订）：块级差异翻译的实质是自研 InnoDB / Oracle 数据块解析器
并保证无损，这是同类公司多年积累的核心资产。在「漏报即安全问题」的前提下，
其正确性无法在可接受成本内证明。第一轮给它标了「悲观 60 人天」，等于承认它可以被排期，这是误导。

**逻辑日志开销的正确论据**（第二轮修订）：第一轮写的是「沙盒是小体量库，开日志开销可控」——
这是错的，沙盒是生产库的克隆，**体量与生产库同级**（假设 A4）。
正确的论据是：**日志开销与「沙盒内的写入量」成正比，而 Agent 的写入量通常远小于库体量**（假设 A5）。
这个论据可测，应在 spike 中量化。

> 日志只在**沙盒库**上开启，源端不做任何改动 —— 与设计红线一致。

## 5. 数据模型

### 5.1 顶层对象

```
ChangeSet
├── changeset_id / sandbox_id / schema_version
├── capture{ path: logical_log|block_diff|hybrid, source, start_position, end_position, captured_at }
├── baseline{ timing_mode, baseline_time, snapshot_id }
├── summary{ 统计信息，供审批 UI 首屏渲染 }
├── ddl_changes[]   # DDL 变更，独立列出（合并风险等级最高）
├── table_changes[] # 按表聚合的 DML 变更
├── conflicts[]     # 冲突标记（首版只标记，不自动解决）
└── integrity{ row_count_checked, checksum, completeness }
```

### 5.2 行级变更表示

每个 `table_changes[i].rows[j]` 为：

```json
{
  "op": "insert | update | delete",
  "pk": { "id": 1024 },
  "before": { "...": "..." },
  "after":  { "...": "..." },
  "changed_columns": ["status", "amount"],
  "txn_id": "0x1f3a",
  "position": "mysql-bin.000007:41522",
  "occurred_at": "2026-09-07T03:12:44Z"
}
```

约定：

- `insert` 必须有 `after`、无 `before`；`delete` 必须有 `before`、无 `after`；`update` 两者都有。
- `changed_columns` 仅对 `update` 有意义，且必须是 `before`/`after` 实际差异列的**完整**集合。
- `pk` 是行定位的唯一依据。**无主键表**是首版明确的能力边界，见 6.3。
- 每个 `pk` 在同一张表内**最多出现一次**（净状态差的直接推论，见第 3 节折叠规则）。
- `position` / `txn_id` / `occurred_at` 是**可选的溯源信息**，供审计追溯；
  **不参与**合并排序，**不参与** A/B 路径比对。块级差异路径下它们恒为 null，这是正常的，不是缺陷。

### 5.3 DDL 变更表示

DDL 单独成表，因为它的风险等级完全不同（不可逆、可能级联影响生产结构）：

```json
{
  "ddl_type": "create_table | drop_table | alter_table | create_index | other",
  "object": "APP.ORDERS",
  "statement": "ALTER TABLE APP.ORDERS ADD COLUMN memo VARCHAR(200)",
  "reversible": false,
  "risk": "high"
}
```

首版策略（**第二轮修订**）：`risk=high` 的 DDL **不自动放行，但也不阻断整个变更集**。

第一轮的规则是「含高风险 DDL → 整个变更集拒绝」，与另一条规则「只支持整变更集全量处理」叠加后，
结果是 **Agent 只要碰过一次 DDL，整个变更集就被门禁拦死**——而 schema 变更恰恰是同类产品
（PlanetScale / Supabase）最主要的价值场景。单看每条规则都合理，组合起来产品就不可用了。

修订后的规则：

| DDL 风险 | 首版处理 |
| --- | --- |
| `low` / `medium` | 随变更集正常流转，审批时展示 |
| `high` | 变更集**仍可流转**，但标记 `requires_explicit_ddl_ack=true`，审批人必须逐条确认后方可放行 |
| 任意 DDL + 直接写生产 | **首版不提供**（见 [05 文档](05-approval-and-permissions.md)：首版为变更导出） |

「拒绝」与「需要额外确认」是两件事，第一轮把它们混为一谈。

### 5.4 冲突标记

**第二轮修订：冲突检测由表级时间戳改为行级乐观并发控制。**

第一轮设计的「表级最后变更时间戳」有两个独立的致命问题：

1. **不可实现**：主流数据库缺乏可靠的「表最后修改时间」。Oracle 的 `DBA_TAB_MODIFICATIONS` 是近似值
   且定期刷新；MySQL InnoDB 的 `information_schema.tables.UPDATE_TIME` 众所周知不可靠（假设 B4 已证伪）；
2. **即使可实现也不可用**：对一张持续写入的核心业务表，「生产库在基线之后有变更」**永远为真**，
   于是每次合并都被拒绝——这个「保守策略」在最重要的场景里等于产品不可用（假设 B5）。

修订后：**比对待处理行在生产库的当前值是否等于沙盒记录的 `before` 镜像**（乐观并发控制的标准做法）。
它不比表级时间戳难实现，却是可实现且可用的。

```json
{
  "level": "row",
  "table": "APP.ORDERS",
  "pk": { "id": 1024 },
  "reason": "production_row_changed_after_baseline",
  "production_current": { "status": "SHIPPED" },
  "sandbox_before": { "status": "PENDING" },
  "baseline_time": "2026-09-07T03:00:00Z"
}
```

`level=table` 保留用于**结构级**冲突（如生产库已 DROP 该表），不再用于「表在基线后被写过」这种判断。
`reason` 允许值见 [`schema/changeset.schema.json`](schema/changeset.schema.json)。

### 5.5 完整性声明（支撑「漏报 = 0」）

`integrity` 是这份契约里最重要的安全字段：

```json
{
  "completeness": "complete | truncated | unknown",
  "reason": null,
  "rows_captured": 1832,
  "bytes_scanned": 91234,
  "checksum": "sha256:..."
}
```

硬规则：`completeness != "complete"` 的 ChangeSet **禁止进入审批与合并流程**，
必须直接判定为失败并要求重建沙盒。差异漏报是安全问题，不是精度问题。

> 注意：`completeness=complete` 是**采集侧的自我声明**，不构成对完整性的独立证明。
> 真正的证据来自 [02 文档](02-spike-ab-plan.md)的 fixture 全集比对，见 [08 文档](08-poc-acceptance.md)指标口径。

## 6. 明确的能力边界（首版不做）

| 边界 | 首版行为 | 后续演进 |
| --- | --- | --- |
| 6.1 直接写生产库 | **首版不做**，改为变更导出，见 [05 文档](05-approval-and-permissions.md) | 待有真实客户案例后再评估 |
| 6.2 部分（按表/按行）导出 | 只支持整变更集导出或全部丢弃 | 待客户审批颗粒度诉求验证（开放问题 #7） |
| 6.3 行级三方合并 | 不做，冲突只标记 | 参照 Dolt 语义，需要行级版本号支撑 |
| 6.4 无主键表 | ChangeSet 标记 `completeness=unknown` 并拒绝流转 | 可考虑 ROWID/物理定位，但跨库语义不统一 |
| 6.5 LOB / 大字段 | 只记录变更标志与长度，不落全量内容 | 需与审计留痕要求一起评估 |
| 6.6 跨库/分布式事务 | TDSQL 等分布式库单独评估（P2） | 见开放问题清单 |
| 6.7 块级差异采集路径 | **首版范围外**（见第 4 节） | 正确性可证明后再评估 |

## 7. 交付物索引

- JSON Schema：[`schema/changeset.schema.json`](schema/changeset.schema.json)
- 示例（MySQL 逻辑日志路径）：[`examples/changeset.mysql.example.json`](examples/changeset.mysql.example.json)
- 示例（块级差异路径，同一组操作）：[`examples/changeset.block-diff.example.json`](examples/changeset.block-diff.example.json)
- 示例（冲突 + 不完整，应被拒绝）：[`examples/changeset.conflict.example.json`](examples/changeset.conflict.example.json)
- 校验脚本：[`tools/validate_changeset.py`](tools/validate_changeset.py)
