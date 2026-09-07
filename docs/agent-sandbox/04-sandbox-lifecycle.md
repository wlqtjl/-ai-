# 04 · 沙盒状态机与生命周期设计

> 对应计划「可立即产出 · 第 4 项」，攻关「难点 3：高频短生命周期并发模型」与「难点 5：资源与凭据安全」。
> 机器可读定义：[`schema/sandbox-state-machine.json`](schema/sandbox-state-machine.json)
> 一致性校验：[`tools/validate_state_machine.py`](tools/validate_state_machine.py)
>（该脚本是**契约自洽性检查**，不构成对实现行为的验证）
> **第二轮复核修订**：首版为「变更导出」，`merging`/`merged` 改名为 `exporting`/`exported`。

## 0. 本文依赖的假设

| 假设 | 类型 | 若不成立 |
| --- | --- | --- |
| [D1](00-assumptions.md) UCDM 更新回切可用于短生命周期沙盒 | **假设** | 仅影响第二版的合并能力，首版导出不依赖它 |
| [A2](00-assumptions.md) 数据通路可支撑目标并发 | **假设（有反证）** | TTL 与配额参数需按实测重定 |

## 1. 状态定义

| 状态 | 含义 | 是否可连接 | 是否占用配额 |
| --- | --- | --- | --- |
| `creating` | 克隆 + 挂载 + 一致性校验进行中 | 否 | 是 |
| `ready` | 可读写，Agent 正常工作态 | 是 | 是 |
| `checkpointing` | 正在创建检查点（「标记」） | 否（短暂） | 是 |
| `rolling_back` | 正在回滚到检查点 | 否 | 是 |
| `diffing` | 正在生成 ChangeSet | 否 | 是 |
| `pending_review` | 已提交审批，冻结写入 | 只读 | 是 |
| `exporting` | 审批通过，正在产出变更集导出物（**不写生产库**） | 否 | 是 |
| `exported` | 导出完成，等待回收 | 否 | 是 |
| `discarding` | 正在按级联顺序回收 | 否 | 是 |
| `discarded` | 已回收（终态） | 否 | 否 |
| `failed` | 失败（终态，保留现场供排查，但仍受 TTL 约束） | 否 | 是 |

## 2. 状态转换

```
creating ──► ready ──► checkpointing ──► ready
                │  └──► rolling_back ──► ready
                │  └──► diffing ──► ready
                └──► pending_review ──► exporting ──► exported ──► discarding ──► discarded
                          └──(rejected)──► ready
任意非终态 ──► discarding ──► discarded      （TTL 到期 / 用户丢弃 / 配额回收）
任意非终态 ──► failed ──► discarding ──► discarded
```

硬约束：

1. **`pending_review` 冻结写入**：提交审批后沙盒转为只读，否则审批的内容与实际导出的内容会不一致
   （这是一个容易被忽略的 TOCTOU 风险）。
2. **`exporting` 不可中断**：导出过程中禁止丢弃；失败进入 `failed`。
   首版**不写生产库**，因此不存在「回退生产库」这一动作（见 [05 文档 1.2](05-approval-and-permissions.md)）。
3. **`discarded` / `exported` 不可回退**：丢弃不可逆，必须在 MCP 工具描述里对 Agent 明示。
4. **`rolling_back` 会级联清理该检查点之后的所有检查点**，与现有「标记时间点回滚」语义一致。

## 3. TTL 与强制回收（难点 3）

| 规则 | 设计 |
| --- | --- |
| TTL 必填 | 无默认值，缺失即 `400 ttl_required` |
| 到期行为 | 到期即进入 `discarding`，**不询问、不宽限**；`pending_review` 状态例外，见下 |
| `pending_review` 到期 | 不直接销毁（会丢失待审内容），改为延长一个固定宽限期并告警；宽限期再到期则自动拒绝审批并回收 |
| 巡检周期 | 后台回收任务每分钟扫描一次，独立队列，不与业务任务竞争 Worker |
| 孤儿资源 | 回收任务同时对账「实际挂载资源 vs 沙盒记录」，发现孤儿资源即告警并回收 |

## 4. 回收级联顺序（不可调整）

现有使用手册明确的级联规则是：**先删除挂载/灾备任务，再删除物理备份**。沙盒回收必须遵守同一顺序：

```
1. 断开并回收临时凭据（drop user）        ← 先切断访问面
2. 卸载挂载点（umount / iSCSI 注销）
3. 删除挂载任务与相关 Airflow DAG run 记录
4. 删除物理副本/快照
5. 释放配额、写审计、更新状态为 discarded
```

任一步失败：**不跳过后续步骤**，而是标记 `discarding_partial` 并持续重试 + 告警，
因为半回收状态既占资源又可能残留可访问凭据，比失败本身更危险。

## 5. 临时凭据生命周期

| 项 | 规则 |
| --- | --- |
| 创建时机 | 沙盒进入 `ready` 时创建，不提前 |
| 有效期 | `min(ttl_seconds, 凭据最大有效期)`，且不晚于沙盒过期时间 |
| 权限范围 | 仅沙盒库内读写；无跨库、无系统级权限；不授予创建外部连接的权限 |
| 销毁 | 回收流程第 1 步即 drop user，绝不长期存活 |
| 复用 | 禁止跨沙盒复用账号，账号名包含 `sandbox_id` 便于审计溯源 |

## 6. 配额模型（难点 3）

按「调用方 + 类型」两级配额，配额是拒绝服务的第一道闸门，而不是事后告警：

| 维度 | 建议初值（需压测校准） | 超限行为 |
| --- | --- | --- |
| 单调用方并发存活沙盒数 | 20 | `409 quota_exceeded` |
| 全集群并发存活沙盒数 | 100（对应 POC 指标 ≥100） | `503 capacity_unavailable` + `Retry-After` |
| 单调用方每小时创建次数 | 60 | `429`（限流） |
| 单沙盒最大 TTL | 7 天 | `400` |
| `point_in_time` 跨度上限 | 72 小时 | `400`，超过需人工审批 |

`agent` 类型调用方的配额应显著低于 `human`，因为 Agent 的重试行为可能是循环的。

## 7. 与现有能力的映射

| 状态/动作 | 底层 | 定性 |
| --- | --- | --- |
| creating / discarding | 现有快速克隆 + 挂载 + 卸载任务 | 直接复用 |
| checkpointing / rolling_back | 「标记」+「标记时间点回滚」 | 直接复用（MySQL 待确认，见开放问题 #4） |
| diffing | ChangeSet 引擎 | 全新开发 |
| exporting | 只读采集 + 文件产出 | **新开发，但不依赖 UCDM 更新回切**（首版不写生产库）|
| （第二版）merging | UCDM 更新回切 | 扩展复用（开放问题 #1）|
| TTL 巡检 / 配额 | Airflow + Celery 队列 | 扩展改造 |
