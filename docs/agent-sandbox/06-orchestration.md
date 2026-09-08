# 06 · Airflow DAG 骨架与 Celery 队列/配额方案

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

**这份文档在讲「后台任务怎么排队」。**

开沙箱、回收沙箱这些活儿都是后台任务。现有系统已经在跑备份和容灾任务了，
如果沙箱任务和它们抢同一批资源，就会出现「因为有人在测试，导致备份没跑完」——这是事故。

所以要单独排一条队，并且限制同时能开多少个。

**还有一件更要紧的事**：有一份实测数据显示，数据传输通道大约只有 20 MB/s。
而「同时开 100 个沙箱」这个目标，粗算需要接近 1 GB/s，**差了大约 50 倍**。

上一轮我只写了一句「建议部署在同一个机房」，把一个**差两个数量级的硬约束**
写成了部署建议，这是严重低估。现在的处理是：**先实测通道到底有多快，再定对外承诺的数字**，
而不是先写个好看的数字再想办法达成。

给谁看：研发、运维。

---

> 对应计划「可立即产出 · 第 6 项」，攻关「难点 3：高频、小颗粒度、短生命周期的并发模型」。
> 前提假设（需研发确认）：底层为 Apache Airflow + Celery Executor（来自使用手册的任务日志佐证）。
> **第二轮复核修订**：① 带宽约束由「部署建议」升级为架构前置条件（第 1.1 节）；
> ② `sandbox_merge` 改为 `sandbox_export`；③ 脱敏与导出的顺序矛盾已澄清。

## 0. 本文依赖的假设

| 假设 | 类型 | 若不成立 |
| --- | --- | --- |
| [A1/A2](00-assumptions.md) 克隆零拷贝、通路 ≥1 GB/s | **假设（A2 有反证）** | 并发与 SLA 目标整体作废，见 1.1 |
| [D2](00-assumptions.md) 现有 Celery 配置可承载数百短任务 | **假设** | 需架构级改造，非参数调优 |

## 1. 核心判断

现有基线场景是「少量大型副本」，沙盒场景是「数百个短生命周期小沙盒」——
这是**场景本质差异，不是参数调优**。因此设计的第一原则是：

> 沙盒任务与备份/容灾任务在队列层面彻底隔离，任何一侧过载都不能拖垮另一侧。

备份/容灾任务是客户的生产保障底线，沙盒是增值能力；两者抢 Worker 时，**沙盒必须先饿死**。

### 1.1 带宽是架构前置条件，不是部署建议（第二轮修订）

第一轮只在第 6 节写了一句「建议沙盒挂载节点与存储集群同机房」，
把一个**量级级别的架构约束**降格成了部署建议。实际数字是：

| 项 | 数值 |
| --- | --- |
| 白皮书实测 DCM ↔ 挂载端 | **约 20 MB/s** |
| 5 分钟内该链路总吞吐上限 | ≈ 6 GB |
| 100 并发沙盒 × 每个 10 MB/s | 需 1000 MB/s |
| **差距** | **约 50 倍（1-2 个数量级）** |

结论：**在 [02 文档第 7 节](02-spike-ab-plan.md)的数据通路实测完成前，
本文的所有并发数字都只是待验证的候选值，不得对外承诺。**
队列并发上限必须由实测的并发衰减曲线反推，而不是先写一个数字再想办法达成。

若实测证明克隆并非存储侧零拷贝（假设 A1 不成立），本文的整个并发模型需要重做。

## 2. 队列划分

| 队列 | 用途 | 建议并发 | 优先级 | 过载行为 |
| --- | --- | --- | --- | --- |
| `dcm_backup`（现有） | 备份、容灾、回切 | 保持现状 | 最高 | 不受沙盒影响 |
| `sandbox_provision` | 沙盒创建（克隆+挂载+校验） | 8-16（需压测） | 中 | 排队，超阈值返回 `503 capacity_unavailable` |
| `sandbox_ops` | 检查点、回滚、ChangeSet 生成 | 16-32（轻任务） | 中 | 排队 |
| `sandbox_export` | 变更集导出（首版**不写生产库**） | 4-8 | 中 | 排队 |
| `sandbox_reaper` | TTL 巡检与强制回收 | 2 | 最高（沙盒域内） | 永不饿死 |

关键取舍说明：

- `sandbox_export` 是纯读 + 产出文件，不写生产库，因此不必像第一轮的 `sandbox_merge` 那样严格串行。
  若第二版启用合并能力，届时需要一条独立的、并发 1-2 的 `sandbox_merge` 队列；
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
                  └─ verify_consistency  [复用现有一致性校验，校验的是原始数据]
                      └─ apply_masking   [复用现有 TDM 脱敏，默认开启]
                          └─ record_masking_manifest   # 记录哪些列被脱敏
                              └─ create_temp_credentials
                                  └─ mark_ready
```

- 失败处理：任一步失败即触发 `sandbox_recycle`，**不留半成品**；
- `estimate_duration` 必须在 `create_clone` 之前完成并写回，否则 API 无法及时返回预计耗时；
- **第二轮修订**：`verify_consistency` 移到 `apply_masking` **之前**。
  第一轮的顺序是 `apply_masking → verify_consistency`，用意是「校验交付态数据」，
  但一致性校验的参照物是源库，脱敏后必然失配；且该顺序把「脱敏值即最终值」固化进了编排，
  直接导致下游合并失效（见 [05 文档 1.1 节](05-approval-and-permissions.md)）。
  修订后：先校验数据搬运的正确性，再做脱敏这一步不可逆的变换；
- `record_masking_manifest` 是新增节点：记录被脱敏的列清单，供导出物标注
  「哪些值不可直接用于生产」——这是脱敏与变更导出并存的必要条件。

### 3.2 `sandbox_ops`（检查点 / 回滚 / ChangeSet）

```
checkpoint:   freeze_writes -> create_mark -> unfreeze -> record
rollback:     freeze_writes -> rollback_to_mark -> cascade_purge_later_marks -> unfreeze
changeset:    freeze_writes -> capture(logical_log|block_diff) -> normalise -> integrity_check -> persist
```

`integrity_check` 是必经节点：产出 `completeness` 字段，不合格的 ChangeSet 直接标记为不可用于审批。

### 3.3 `sandbox_export`（首版；第一轮为 `sandbox_merge`）

```
revalidate_changeset_checksum   # 防止审批后被替换
  └─ verify_net_diff_invariants # 主键唯一、before != after（01 文档 3.3）
      └─ row_level_conflict_probe   # 只读探测：生产库当前值 vs 沙盒 before 镜像
          └─ render_impact_report
              └─ render_change_script     # 按目标库方言，带脱敏列告警
                  └─ attach_masking_manifest
                      └─ publish_artifact -> mark_exported
```

三点说明：

1. **本 DAG 不写生产库**，`row_level_conflict_probe` 是只读探测；
   因此第一轮的 `create_production_rollback_point` 与 `rollback_production` 节点不再需要；
2. 冲突探测是**行级**乐观并发控制（比对当前值与 before 镜像），
   不是第一轮的表级时间戳（不可实现且不可用，见 05 文档 5.2）；
3. `attach_masking_manifest` 保证导出物明确标注哪些值来自脱敏数据、不可直接写生产。

> 若一牧决策为「首版直接合并回生产」（[开放问题 #9](09-open-questions.md)），
> 则需恢复 `create_production_rollback_point` / `apply_merge` / `rollback_production` 三个节点，
> 并把队列并发改回 1-2，同时必须先解决 05 文档 1.1 的脱敏矛盾。

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
3. 沙盒挂载节点与存储集群是否同机房？**已升级为阶段 0 出口判据**，见 1.1 与 02 文档第 7 节。（网络约 20M/s，明显低于本地 IO，
   跨机房会直接击穿 `near_current` P95 < 5 分钟的目标）
