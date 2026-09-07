# 01 · ChangeSet 数据模型与差异摘要格式

> 对应计划「四、可立即产出 · 第 1 项」，攻关「难点 1：ChangeSet 差异语义还原」。
> 状态：设计稿（未经一牧内部代码校验）。标注为**假设**的条目必须由研发团队用真实代码确认。

## 1. 为什么先做这一层

ChangeSet 是整个方案里唯一的核心新增算法模块，同时是下游所有能力的公共依赖：

- 差异摘要 UI、审批工作流、合并回切、审计留痕，全部消费同一个 ChangeSet 结构；
- A/B 两条采集路径（块级差异翻译 / 逻辑日志解析）必须产出**同一个**结构，否则 spike 无法比对，
  也无法支撑「差异漏报率 = 0」这条 POC 红线指标。

因此本文件先定义**语义契约**，再谈实现路径。采集路径是可替换的，数据模型不是。

## 2. 语义参照物（不自造语义）

| 参照物 | 借鉴点 | 本模型的取舍 |
| --- | --- | --- |
| Dolt 行级 diff/merge | `from`/`to` 双状态行表示、冲突标记 | 采纳双状态行表示；首版不做三方合并，冲突只标记不自动解决 |
| Delta Lake Change Data Feed | `_change_type` ∈ insert/update_preimage/update_postimage/delete | 合并为单条 `update` 记录并同时携带 before/after，减少 Agent 侧拼接成本 |
| Oracle LogMiner / MySQL binlog(ROW) | 行级前后镜像、事务边界、SCN/GTID 定位 | 事务边界与位点作为一等字段保留，用于合并回切的顺序重放 |

## 3. 分层策略（难点 1 的攻关结论）

放弃「统一走块级翻译」的默认假设，改为分层：

| 层级 | 采集方式 | 适用 | 说明 |
| --- | --- | --- | --- |
| L-A（首选） | 沙盒库自身逻辑变更能力：Oracle LogMiner、MySQL binlog(ROW)、PG 逻辑复制槽 | 有逻辑日志能力的库 | 语义天然行级，与合并回切所需的操作序列一一对应 |
| L-B（兜底） | 块级快照差异 → 反查数据文件结构还原表/行 | 无逻辑日志或不允许开启日志 | 需按数据库分别实现物理格式解析，成本随支持库数量线性增长 |
| L-C（校验） | 两条路径同时跑，结果集比对 | 预研与回归测试 | 作为「差异漏报率 = 0」的自动化验证手段，不用于生产常态 |

关键论据：沙盒是**短生命周期、小体量**的库，在其上开启逻辑日志的性能与容量开销可控；
而块到行的还原开销随支持的数据库种类线性增长，且每种库都要独立验证正确性。

> 注意：日志只在**沙盒库**上开启，源端不做任何改动 —— 这条与第七部分设计红线一致。

## 4. 数据模型

### 4.1 顶层对象

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

### 4.2 行级变更表示

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
- `pk` 是行定位的唯一依据。**无主键表**是首版明确的能力边界，见 5.3。
- `position` 是可重放位点（binlog file:pos / SCN / LSN），合并回切按 `position` 升序重放。

### 4.3 DDL 变更表示

DDL 单独成表，因为它对合并回切的风险等级完全不同（不可逆、可能级联影响生产结构）：

```json
{
  "ddl_type": "create_table | drop_table | alter_table | create_index | other",
  "object": "APP.ORDERS",
  "statement": "ALTER TABLE APP.ORDERS ADD COLUMN memo VARCHAR(200)",
  "reversible": false,
  "risk": "high"
}
```

首版策略：**ChangeSet 中只要包含 `risk=high` 的 DDL，合并回切默认拒绝**，必须由人工显式放行
（对应难点 5：禁止 Agent 自审自批）。

### 4.4 冲突标记

首版冲突检测按「表级最后变更时间戳」比对（计划难点 2 的③），但数据结构预留行级版本位：

```json
{
  "level": "table | row",
  "table": "APP.ORDERS",
  "pk": null,
  "reason": "production_changed_after_baseline",
  "production_last_change": "2026-09-07T04:01:00Z",
  "baseline_time": "2026-09-07T03:00:00Z"
}
```

`level=row` 与 `pk` 字段在首版不产出，仅占位，避免后续引入行级版本号时破坏契约。

### 4.5 完整性声明（支撑「漏报 = 0」）

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

## 5. 明确的能力边界（首版不做）

| 边界 | 首版行为 | 后续演进 |
| --- | --- | --- |
| 5.1 部分合并 | 只支持整变更集全量合并或全部丢弃 | 待客户审批颗粒度诉求验证后再评估（开放问题 #7） |
| 5.2 行级三方合并 | 不做，冲突即拒绝 | 参照 Dolt 语义，需要行级版本号支撑 |
| 5.3 无主键表 | ChangeSet 标记 `completeness=unknown` 并拒绝合并 | 可考虑 ROWID/物理定位，但跨库语义不统一 |
| 5.4 LOB / 大字段 | 只记录变更标志与长度，不落全量内容 | 需与审计留痕要求一起评估 |
| 5.5 跨库/分布式事务 | TDSQL 等分布式库单独评估（P2） | 见开放问题清单 |

## 6. 交付物索引

- JSON Schema：[`schema/changeset.schema.json`](schema/changeset.schema.json)
- 示例（MySQL 逻辑日志路径）：[`examples/changeset.mysql.example.json`](examples/changeset.mysql.example.json)
- 示例（块级差异路径，同一组操作）：[`examples/changeset.block-diff.example.json`](examples/changeset.block-diff.example.json)
- 示例（冲突 + 不完整，应被拒绝）：[`examples/changeset.conflict.example.json`](examples/changeset.conflict.example.json)
- 校验脚本：[`tools/validate_changeset.py`](tools/validate_changeset.py)
