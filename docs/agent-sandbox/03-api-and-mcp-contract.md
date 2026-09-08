# 03 · REST API 与 MCP 工具契约

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

**这份文档在定义「Agent 能点哪几道菜」。**

给 AI 用的功能菜单叫 MCP。菜单上写着什么，AI 就只能干什么；菜单上没有的，它想干也干不了。

建议菜单上只放 5 项：申请沙箱、查状态、看改了什么、提交审批、扔掉不要。

**菜单上刻意不放两样东西：**

1. **不放「执行 SQL」**——AI 拿到沙箱后用普通方式自己连数据库。原因是：查询结果动辄上百万行，
   塞进 AI 会撑爆还烧钱；而且一旦 AI 被骗（有人在数据里藏一句「把表删了」，AI 真会照做），
   它就能执行任意 SQL，而系统没法判断一条 SQL 是好是坏。**能干的事越少，被骗之后损失越小。**
2. **不放「审批通过」**——AI 只能提交申请，不能批准自己的申请。

另外接口要老实：开一个沙箱可能要几分钟，也可能要几小时（取决于要不要回放日志）。
所以必须**返回预估时间和原因**，不能只回一句「处理中」。

给谁看：研发（照着实现）、产品（理解能力边界）。

---

> 对应计划「可立即产出 · 第 3 项」，攻关「难点 4：SLA 的双路径承诺」与「难点 5：Agent 侧风险面」。
> 交付物：[`schema/openapi.yaml`](schema/openapi.yaml)、[`schema/mcp-tools.json`](schema/mcp-tools.json)
> **第二轮复核修订**：`merge-requests` 改名 `change-requests`，新增导出物接口；
> 首版**没有任何接口会写生产库**（见 [05 文档 1.2](05-approval-and-permissions.md)）。

## 0. 本文依赖的假设

| 假设 | 类型 | 若不成立 |
| --- | --- | --- |
| [A1/A2](00-assumptions.md) 数据通路可支撑目标耗时 | **假设（A2 有反证）** | `estimated_ready_at` 的估算模型需重做 |
| [C2](00-assumptions.md) 客户有既有变更执行流程 | **假设** | 导出物无处落地，需自建执行侧 |

## 1. 设计原则

1. **异步优先**：沙盒创建耗时在 `near_current` 与 `point_in_time` 两种模式下相差一个数量级，
   所有创建类接口一律返回 `202 + task_id`，由客户端轮询，**禁止**同步阻塞返回。
2. **禁止统一 SLA**：创建响应必须返回 `estimated_ready_at` 与 `estimate_basis`
   （`snapshot_hit` / `log_replay`），把耗时不确定性显式暴露给调用方而不是藏起来。
3. **Agent 不可自审自批**：审批类接口只接受 `principal_type=human` 的凭据，
   MCP 工具层**不提供** approve 能力（见 05 文档）。
4. **TTL 强制必填**：`ttl_seconds` 无默认值，缺失即 `400`，杜绝沙盒资源泄漏。
5. **幂等**：所有写操作接受 `Idempotency-Key` 请求头，重复提交返回首次结果。

## 2. REST 资源模型

```
POST   /v1/sandboxes                      创建沙盒（异步）
GET    /v1/sandboxes                      列表（支持按 requester/state/purpose_tag 过滤）
GET    /v1/sandboxes/{id}                 查询状态与连接信息
DELETE /v1/sandboxes/{id}                 丢弃并回收（同步触发，异步完成）
POST   /v1/sandboxes/{id}/checkpoints     创建检查点（复用「标记」）
GET    /v1/sandboxes/{id}/checkpoints     列出检查点
POST   /v1/sandboxes/{id}/rollback        回滚到指定检查点（复用「标记时间点回滚」）
GET    /v1/sandboxes/{id}/changeset       获取 ChangeSet（见 01 文档）
POST   /v1/sandboxes/{id}/change-requests  提交审批（Agent 可调用）
GET    /v1/change-requests/{id}            查询审批状态
POST   /v1/change-requests/{id}/dry-run    导出干跑（只读），返回影响面报告
POST   /v1/change-requests/{id}/approve    审批通过并触发导出（仅人类）
POST   /v1/change-requests/{id}/reject     审批拒绝（仅人类）
GET    /v1/change-requests/{id}/export     下载变更集导出物（首版最终交付形式）
GET    /v1/tasks/{id}                     异步任务轮询
```

## 3. 关键字段

### 3.1 创建请求

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `source_id` | 是 | 源库/备份集标识，沿用 DCM 现有标识体系 |
| `timing_mode` | 是 | `near_current`（命中最近快照，分钟级）/ `point_in_time`（可能需日志回放） |
| `point_in_time` | 条件 | `timing_mode=point_in_time` 时必填 |
| `ttl_seconds` | 是 | 强制必填，无默认值；上限由配额策略决定 |
| `mount_profile` | 否 | 轻量化资源模板，默认 `sandbox-small`，**不**沿用源端生产参数 |
| `masking_policy` | 否 | 脱敏策略，**默认开启**（`default`），显式传 `none` 需要额外授权（难点 5） |
| `purpose_tag` | 是 | 用途标签，进审计；Agent 调用时必须携带 |
| `requester_type` | 自动 | 由凭据推导，`human` / `agent`，不接受客户端自报 |

### 3.2 创建响应（202）

```json
{
  "sandbox_id": "sbx-7f3a91",
  "task_id": "task-88231",
  "state": "creating",
  "timing_mode": "point_in_time",
  "estimate_basis": "log_replay",
  "estimated_ready_at": "2026-09-07T05:40:00Z",
  "estimate_confidence": "low",
  "poll_after_seconds": 30
}
```

`estimate_basis` 的取值直接对应 9.1 节的双路径性能模型：

| estimate_basis | 含义 | 估算方法 |
| --- | --- | --- |
| `snapshot_hit` | 命中最近快照，无需日志回放 | 按历史 P95 给固定值 |
| `log_replay` | 需回放 N 小时日志 | 经验公式 ≈ N/2 小时，`estimate_confidence=low` |

### 3.3 错误码

| HTTP | code | 场景 |
| --- | --- | --- |
| 400 | `ttl_required` | 未提供 `ttl_seconds` |
| 400 | `point_in_time_required` | 模式与参数不匹配 |
| 403 | `agent_cannot_approve` | Agent 凭据调用审批接口（难点 5 强制点） |
| 403 | `masking_bypass_denied` | 无授权却请求关闭脱敏 |
| 409 | `quota_exceeded` | 超出调用方并发沙盒配额 |
| 409 | `sandbox_state_conflict` | 状态机不允许的转换（见 04 文档） |
| 409 | `row_level_conflict_detected` | 行级乐观并发冲突：生产库当前值 ≠ 沙盒 before 镜像（第一轮的表级时间戳方案已废弃，见 05 文档 5.2）|
| 409 | `export_not_ready` | 审批未通过或导出仍在进行中 |
| 422 | `changeset_incomplete` | `integrity.completeness != complete`，禁止提交审批 |
| 422 | `high_risk_ddl_requires_human_ack` | 含高风险 DDL 需人工显式放行 |
| 503 | `capacity_unavailable` | 沙盒队列积压超阈值，建议重试时间在 `Retry-After` |

## 4. MCP 工具层（供 Agent / LLM 工作流调用）

只暴露 4 个工具，**刻意不暴露审批与合并执行**：

| 工具 | 能力 | 为什么这样切 |
| --- | --- | --- |
| `request_data_sandbox` | 申请沙盒 | Agent 的起点 |
| `get_sandbox_diff` | 获取 ChangeSet 摘要 | Agent 自检变更是否符合预期 |
| `submit_sandbox_for_review` | 提交审批 | Agent 只能「提交」，不能「通过」 |
| `discard_sandbox` | 丢弃沙盒 | 保证 Agent 能自行止损与释放资源 |

安全约束（写进工具描述，也在服务端强制）：

- 服务端从凭据推导 `requester_type=agent`，**忽略**参数里的任何身份声明，防止提示注入伪造身份；
- `approve` / `reject` 在 MCP 层不存在，Agent 无法通过任何工具组合达成自审自批；
- 首版**根本不存在**写生产库的接口，这既是安全设计也是对外话术的一部分；
- `get_sandbox_diff` 返回的是**结构化摘要**，并对超限结果返回 `truncated=true` 而非静默截断；
- 所有工具调用带 `purpose_tag`，落审计。

工具的 JSON Schema 见 [`schema/mcp-tools.json`](schema/mcp-tools.json)。

## 5. 与现有能力的映射（需研发确认）

| 接口 | 底层复用 | 确认点 |
| --- | --- | --- |
| 创建/销毁 | 现有快速克隆 + 挂载任务 | 轻量 `mount_profile` 是否已支持 |
| 检查点/回滚 | 「标记」+「标记时间点回滚」 | MySQL 是否有等价能力（开放问题 #4） |
| changeset | E2 新增 | 路径 A/B 未定（开放问题 #6） |
| export | 只读采集 + 文件产出 | 首版不依赖 UCDM 更新回切；第二版若做合并再确认（开放问题 #1）|
| 临时凭据 | 现有账号创建模式 | CDB/非 CDB 是否已封装内部 API（开放问题 #5） |
