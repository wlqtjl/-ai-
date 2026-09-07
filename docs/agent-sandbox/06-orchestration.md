# 06 · Airflow DAG 骨架与 Celery 队列/配额方案

> 对应计划「可立即产出 · 第 6 项」，攻关「难点 3：高频、小颗粒度、短生命周期的并发模型」。
> 前提假设（需研发确认）：底层为 Apache Airflow + Celery Executor（来自使用手册的任务日志佐证）。

## 1. 核心判断

现有基线场景是「少量大型副本」，沙盒场景是「数百个短生命周期小沙盒」——
这是**场景本质差异，不是参数调优**。因此设计的第一原则是：

> 沙盒任务与备份/容灾任务在队列层面彻底隔离，任何一侧过载都不能拖垮另一侧。

备份/容灾任务是客户的生产保障底线，沙盒是增值能力；两者抢 Worker 时，**沙盒必须先饿死**。

## 2. 队列划分

| 队列 | 用途 | 建议并发 | 优先级 | 过载行为 |
| --- | --- | --- | --- | --- |
| `dcm_backup`（现有） | 备份、容灾、回切 | 保持现状 | 最高 | 不受沙盒影响 |
| `sandbox_provision` | 沙盒创建（克隆+挂载+校验） | 8-16（需压测） | 中 | 排队，超阈值返回 `503 capacity_unavailable` |
| `sandbox_ops` | 检查点、回滚、ChangeSet 生成 | 16-32（轻任务） | 中 | 排队 |
| `sandbox_merge` | 合并回切执行 | **1-2（严格串行）** | 高 | 排队，不并发 |
| `sandbox_reaper` | TTL 巡检与强制回收 | 2 | 最高（沙盒域内） | 永不饿死 |

关键取舍说明：

- `sandbox_merge` 并发压到最低，因为它是唯一会写生产库的路径，串行化能大幅降低事故面；
- `sandbox_reaper` 优先级最高，否则「回收任务排在创建任务后面」会导致资源泄漏与雪崩；
- `sandbox_provision` 与 `sandbox_ops` 分开，是因为创建是重 IO 任务、检查点是轻任务，
  混在一起会让轻任务被重任务饿死，直接破坏 Agent 的交互体验。

## 3. DAG 设计

按沙盒生命周期拆成 4 个 DAG，而不是一个巨型 DAG——因为它们的触发时机、并发度、失败语义都不同。

### 3.1 `sandbox_provision`

```
resolve_timing_mode        # near_current 命中快照 / point_in_time 需回放
  └─ estimate_duration     # 产出 estimate_basis + estimated_ready_at（写回 API）
      └─ allocate_quota    # 配额闸门，超限直接失败，不排队占资源
          └─ create_clone           [复用现有快速克隆]
              └─ mount_target       [复用现有挂载]
                  └─ apply_masking  [复用现有 TDM 脱敏，默认开启]
                      └─ verify_consistency  [复用现有一致性校验]
                          └─ create_temp_credentials
                              └─ mark_ready
```

- 失败处理：任一步失败即触发 `sandbox_recycle`，**不留半成品**；
- `estimate_duration` 必须在 `create_clone` 之前完成并写回，否则 API 无法及时返回预计耗时；
- `apply_masking` 放在 `verify_consistency` 之前，确保校验的是交付态数据。

### 3.2 `sandbox_ops`（检查点 / 回滚 / ChangeSet）

```
checkpoint:   freeze_writes -> create_mark -> unfreeze -> record
rollback:     freeze_writes -> rollback_to_mark -> cascade_purge_later_marks -> unfreeze
changeset:    freeze_writes -> capture(logical_log|block_diff) -> normalise -> integrity_check -> persist
```

`integrity_check` 是必经节点：产出 `completeness` 字段，不合格的 ChangeSet 直接标记为不可用于审批。

### 3.3 `sandbox_merge`

```
revalidate_changeset_checksum   # 防止审批后被替换
  └─ conflict_detection         # 时间戳比对
      └─ create_production_rollback_point   [复用 RBA]
          └─ dry_run
              └─ apply_merge    [复用 UCDM 更新回切]
                  └─ verify
                      └─ (失败) rollback_production -> mark_failed
```

`create_production_rollback_point` 是不可跳过的前置节点——它是「生产库受影响次数 = 0」这条
POC 红线指标的技术保障。

### 3.4 `sandbox_reaper`（定时，每分钟）

```
scan_expired -> enqueue_recycle
scan_orphans -> alert + enqueue_recycle       # 实际资源与沙盒记录对账
scan_stuck    -> alert                        # 长时间停留在中间态的沙盒
```

回收严格按 04 文档第 4 节的级联顺序执行，失败不跳步，标记 `discarding_partial` 持续重试。

## 4. Celery 配置要点

| 配置 | 建议 | 理由 |
| --- | --- | --- |
| Worker 分组 | 沙盒队列由独立 Worker 进程组消费 | 与备份任务物理隔离 |
| `worker_prefetch_multiplier` | 1（沙盒队列） | 长任务预取会造成严重不均衡 |
| `task_acks_late` | true | Worker 崩溃后任务可重投，避免沙盒卡死在中间态 |
| 任务超时 | 按队列分别设置；`sandbox_provision` 的 `point_in_time` 模式需按跨度动态计算 | 统一超时会误杀长回放任务 |
| 重试 | 创建类可重试；**合并类不自动重试**（必须人工介入） | 自动重试写生产是危险行为 |
| 结果后端保留期 | 短（小时级） | 沙盒任务量大，结果堆积会拖垮后端 |

## 5. 配额执行点

配额必须在 **DAG 之前**（API 层）和 **DAG 之内**（`allocate_quota`）各校验一次：

- API 层校验给调用方**快速失败**的体验；
- DAG 内校验防止并发竞态导致超发；
- 两处使用同一份配额定义（见 04 文档第 6 节）。

## 6. 压测前必须确认的问题

1. 现有 Celery Worker 的并发配置与队列积压策略是什么？（开放问题 #2）
2. 新增 5 个队列对现有 Airflow 调度器的元数据库压力有多大？（`sandbox_reaper` 每分钟触发，
   会显著增加 DAG run 记录量，可能需要缩短元数据保留期）
3. 沙盒挂载节点与存储集群是否同机房？（网络约 20M/s，明显低于本地 IO，
   跨机房会直接击穿 `near_current` P95 < 5 分钟的目标）
