# 03 · REST API 与 MCP 工具契约

> 对应计划「可立即产出 · 第 3 项」，攻关「难点 4：SLA 的双路径承诺」与「难点 5：Agent 侧风险面」。
> 交付物：[`schema/openapi.yaml`](schema/openapi.yaml)、[`schema/mcp-tools.json`](schema/mcp-tools.json)

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
POST   /v1/sandboxes/{id}/merge-requests  提交审批（Agent 可调用）
GET    /v1/merge-requests/{id}            查询审批状态
POST   /v1/merge-requests/{id}/dry-run    合并干跑，返回影响面报告（难点 2 ①）
POST   /v1/merge-requests/{id}/approve    审批通过（仅人类）
POST   /v1/merge-requests/{id}/reject     审批拒绝（仅人类）
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
| 409 | `merge_conflict_detected` | 时间戳冲突检测未通过 |
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
- `approve` / `reject` / `merge` 在 MCP 层不存在，Agent 无法通过任何工具组合达成自审自批；
- `get_sandbox_diff` 返回的是**结构化摘要**，并对超限结果返回 `truncated=true` 而非静默截断；
- 所有工具调用带 `purpose_tag`，落审计。

工具的 JSON Schema 见 [`schema/mcp-tools.json`](schema/mcp-tools.json)。

## 5. 与现有能力的映射（需研发确认）

| 接口 | 底层复用 | 确认点 |
| --- | --- | --- |
| 创建/销毁 | 现有快速克隆 + 挂载任务 | 轻量 `mount_profile` 是否已支持 |
| 检查点/回滚 | 「标记」+「标记时间点回滚」 | MySQL 是否有等价能力（开放问题 #4） |
| changeset | E2 新增 | 路径 A/B 未定（开放问题 #6） |
| merge | UCDM 更新回切 | 临时沙盒是否走同一代码路径（开放问题 #1） |
| 临时凭据 | 现有账号创建模式 | CDB/非 CDB 是否已封装内部 API（开放问题 #5） |
