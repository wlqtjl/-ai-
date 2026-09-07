# 02 · A/B 路径对照 Spike 实验方案

> 对应计划「阶段 0 · 技术预研」与「可立即产出 · 第 2 项」，攻关「难点 1」。
> 目的：用可复现的证据把 E2（ChangeSet 差异引擎）的估算依据等级从 **L3（路线未定）** 降到 **L2（路线明确）**。

## 1. 时间盒与出口判据

| 项 | 约定 |
| --- | --- |
| 时间盒 | 路径 A 5 人天 + 路径 B 5 人天，硬性上限，超时即判定该路径「首版不可用」 |
| 首选实验库 | **MySQL**（binlog 成本最低、工具链最成熟），Oracle 作为第二轮 |
| 出口判据 1 | 在标准操作序列下，A/B 两条路径产出的 ChangeSet **语义等价**（用比对脚本判定） |
| 出口判据 2 | 任一路径能稳定给出 `integrity.completeness = complete` |
| 出口判据 3 | 单次采集耗时、额外存储开销、对沙盒库 TPS 的影响三项数据齐备 |
| 失败处理 | 两条路径都不满足出口判据 → 项目维持预研状态，不进入阶段 1 |

## 2. 两条路径的定义

| 路径 | 实现 | 需要预先确认 |
| --- | --- | --- |
| **A · 块级差异翻译** | 读取 COW 快照的块级变化，反查数据文件结构还原到表/行 | 是否已有 InnoDB / Oracle 数据块解析代码可复用（开放问题 #6） |
| **B · 逻辑日志解析** | 在**沙盒库**上开启 binlog(ROW, full image)/LogMiner，解析为行级变更 | 沙盒模板是否允许默认开启日志；额外空间与 TPS 开销 |

> 红线复核：路径 B 的日志只在沙盒库开启，**源端零改动**。任何需要在源端开日志的方案直接出局。

## 3. 标准操作序列（fixture）

固定脚本见 [`spike/fixtures/workload.mysql.sql`](spike/fixtures/workload.mysql.sql)，
覆盖以下必须被正确还原的语义：

| # | 操作 | 考察点 |
| --- | --- | --- |
| 1 | 单行 INSERT | 基础 insert，after 镜像完整性 |
| 2 | 单行 UPDATE（改 2 列） | before/after 双镜像与 `changed_columns` 精确性 |
| 3 | 单行 DELETE | before 镜像是否保留（块级路径的典型弱项） |
| 4 | 批量 UPDATE（影响 500 行） | 行数规模下的完整性与性能 |
| 5 | 同一行被连续修改 3 次 | 是否正确折叠为净变更 or 保留操作序列（须与合并回切语义一致） |
| 6 | 插入后在同一事务内删除 | 净效果为零的变更是否被误报 |
| 7 | 回滚的事务 | **必须不出现在 ChangeSet 里**（块级路径的高风险点） |
| 8 | `ALTER TABLE ADD COLUMN` | DDL 识别与风险定级 |
| 9 | 无主键表的 UPDATE | 是否正确降级为 `completeness=unknown` 而不是静默漏报 |
| 10 | 大字段（TEXT/BLOB）更新 | 边界 5.4 行为是否一致 |

每条操作都在 fixture 中标注了**期望的 ChangeSet 语义**，便于人工核对。

## 4. 比对方法

比对脚本：[`spike/compare_changesets.py`](spike/compare_changesets.py)

比对时**规范化**掉两条路径天然不同、且不影响合并语义的字段：

- 行顺序（块级路径无时序，按 `表 + 主键 + op` 排序后比对）；
- `txn_id` / `position` / `occurred_at`（块级路径拿不到，允许为 null）；
- `changed_columns` 的顺序；
- `checksum` / `bytes_scanned` / `captured_at` / `changeset_id`。

**不允许**存在差异的部分（任何差异都判定实验失败）：

- 表集合、每表的 insert/update/delete 计数；
- 每一行的 `op`、`pk`、`before`、`after`、`changed_columns` 集合；
- `ddl_changes` 的 `ddl_type` / `object` / `risk`；
- `integrity.completeness`。

脚本输出三类结论：`EQUIVALENT`（等价）、`MISSING`（某路径漏报，最严重）、`MISMATCH`（值不一致）。

**漏报优先级**：只要出现 `MISSING`，无论数量多少，该路径直接判负——这正是 POC 红线指标
「ChangeSet 差异摘要与实际变更一致率 = 100%」的来源。

## 5. 需要同时记录的量化指标

| 指标 | 路径 A | 路径 B | 用途 |
| --- | --- | --- | --- |
| 采集单次耗时（10 万行变更） | | | E2 性能可行性 |
| 额外存储开销（占沙盒体量比例） | | | 容量规划 |
| 对沙盒库 TPS 的影响 | | | 路径 B 的主要成本项 |
| 新增代码量估计（按库） | | | 判断「成本是否随支持库数量线性增长」 |
| 能覆盖的数据库数量（不新增开发） | | | 跨库统一性 |

## 6. 结论模板（实验结束时填写）

```
选定路径：A / B / 混合（B 为主 + A 兜底）
理由：
未通过的 fixture 项：
E2 估算依据等级：L3 -> L__
E2 三点估计（乐观/最可能/悲观，人天）：
遗留风险：
```

## 7. 运行方式

```bash
# 仅比对两份已产出的 ChangeSet
python3 spike/compare_changesets.py path/to/a.json path/to/b.json

# 用仓库内示例做回归自测（含注入漏报/不一致的负样本）
python3 spike/compare_changesets.py --self-test
```
