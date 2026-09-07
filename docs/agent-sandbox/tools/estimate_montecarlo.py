#!/usr/bin/env python3
"""工作量合计的蒙特卡洛模拟。

【为什么需要这个脚本】
第一轮的 10 文档把各 Epic 的乐观值直接相加得 76、悲观值相加得 265。**这在统计上是错的**：
它隐含「所有 Epic 同时命中最好 / 最坏情况」，而这些事件近似独立，同时全中的概率接近于零。
真实的 P10/P90 区间要窄得多。期望值可以相加（PERT 期望的线性性质成立），区间不可以。

本脚本对每个 Epic 按 PERT-Beta 分布抽样后求和，给出合计的 P10 / P50 / P90。

注意：模拟假设各 Epic 相互**独立**。若存在共同的失败因子（例如「数据通路带宽不足」
会同时拖慢 E1/E9），真实区间会比模拟结果更宽。这一点必须在引用数字时一并说明。

用法：
    python3 tools/estimate_montecarlo.py            # 默认 100000 次抽样
    python3 tools/estimate_montecarlo.py --self-test
"""

from __future__ import annotations

import argparse
import random
import statistics

# (Epic, 乐观 O, 最可能 M, 悲观 P, 是否计入首版合计)
EPICS: list[tuple[str, float, float, float, bool]] = [
    ("E1 沙盒编排核心（双路径）", 8, 13, 22, True),
    ("E2 ChangeSet 差异引擎（仅逻辑日志路径）", 8, 13, 24, True),
    ("E3 REST API 与鉴权", 8, 13, 20, True),
    ("E4 MCP 工具封装", 6, 9, 14, True),
    ("E5 内置审批 UI", 12, 18, 28, True),
    ("E6 变更集导出器（含行级冲突探测）", 6, 10, 18, True),
    ("E7 配额与治理（含回收顺序）", 6, 9, 14, True),
    ("E8 审计日志扩展", 4, 6, 10, True),
    ("E9 并发压测与调优", 6, 12, 25, True),
    ("E10 客户审批系统集成", 5, 9, 15, False),
    ("E11 MySQL「标记/回滚」补齐", 0, 10, 16, True),
    ("E12 脱敏默认开启接入", 3, 5, 8, True),
    ("E13 基准测试套件", 5, 8, 13, True),
    ("E14 数据通路实测（阶段 0）", 1, 2, 4, True),
]

# PERT-Beta 的形状参数，lambda=4 是标准取值
PERT_LAMBDA = 4.0


def pert_expected(o: float, m: float, p: float) -> float:
    return (o + PERT_LAMBDA * m + p) / (PERT_LAMBDA + 2)


def sample_pert(rng: random.Random, o: float, m: float, p: float) -> float:
    """按 PERT-Beta 分布抽一个样本。"""
    if p <= o:
        return o
    mean = pert_expected(o, m, p)
    # 由均值反推 Beta 分布的两个形状参数
    if abs(mean - m) < 1e-9:
        alpha = beta = 1.0 + PERT_LAMBDA / 2.0
    else:
        alpha = ((mean - o) * (2 * m - o - p)) / ((m - mean) * (p - o))
        beta = alpha * (p - mean) / (mean - o)
    if alpha <= 0 or beta <= 0:
        alpha = beta = 1.0 + PERT_LAMBDA / 2.0
    return o + rng.betavariate(alpha, beta) * (p - o)


def simulate(trials: int = 100000, seed: int = 20260907) -> dict[str, float]:
    rng = random.Random(seed)
    included = [e for e in EPICS if e[4]]
    totals = [sum(sample_pert(rng, o, m, p) for _, o, m, p, _ in included) for _ in range(trials)]
    totals.sort()

    def pct(q: float) -> float:
        idx = min(len(totals) - 1, max(0, int(q * len(totals))))
        return totals[idx]

    return {
        "trials": trials,
        "sum_of_optimistic": sum(o for _, o, _, _, _ in included),
        "sum_of_pessimistic": sum(p for _, _, _, p, _ in included),
        "sum_of_pert_expected": sum(pert_expected(o, m, p) for _, o, m, p, _ in included),
        "mean": statistics.fmean(totals),
        "p10": pct(0.10),
        "p50": pct(0.50),
        "p90": pct(0.90),
    }


def report(trials: int) -> int:
    print("Epic 明细（PERT 期望 = (O + 4M + P) / 6）")
    for name, o, m, p, included in EPICS:
        mark = " " if included else "*"
        print(f" {mark}{name:<38} O={o:<5} M={m:<5} P={p:<5} 期望={pert_expected(o, m, p):.1f}")
    print(" * 号项目不计入首版合计")

    r = simulate(trials)
    print(f"\n蒙特卡洛（{r['trials']} 次抽样，各 Epic 独立）")
    print(f"  P10  : {r['p10']:.0f} 人天")
    print(f"  P50  : {r['p50']:.0f} 人天")
    print(f"  P90  : {r['p90']:.0f} 人天")
    print(f"  均值 : {r['mean']:.0f} 人天（与 PERT 期望之和 {r['sum_of_pert_expected']:.0f} 应接近）")
    print("\n对照：第一轮的简单相加（统计上错误的口径）")
    print(f"  乐观值直接相加 : {r['sum_of_optimistic']:.0f} 人天  ← 隐含所有 Epic 同时命中最好情况")
    print(f"  悲观值直接相加 : {r['sum_of_pessimistic']:.0f} 人天  ← 隐含所有 Epic 同时命中最坏情况")
    print("\n提醒：模拟假设各 Epic 独立。存在共同失败因子时，真实区间比上表更宽。")
    return 0


def self_test() -> int:
    failures = 0
    r = simulate(trials=20000, seed=1)

    # 1. 蒙特卡洛区间必须严格窄于简单相加的区间——这正是第一轮求和谬误的核心
    if not (r["sum_of_optimistic"] < r["p10"] and r["p90"] < r["sum_of_pessimistic"]):
        print(f"[SELFTEST FAIL] P10/P90 本应严格落在简单相加区间之内：{r}")
        failures += 1

    # 2. 均值应与 PERT 期望之和接近（期望值可以相加，区间不可以）
    if abs(r["mean"] - r["sum_of_pert_expected"]) > 2.0:
        print(f"[SELFTEST FAIL] 均值 {r['mean']:.1f} 与 PERT 期望之和 {r['sum_of_pert_expected']:.1f} 相差过大")
        failures += 1

    # 3. 分位数单调
    if not r["p10"] < r["p50"] < r["p90"]:
        print("[SELFTEST FAIL] 分位数非单调")
        failures += 1

    # 4. 单个 Epic 的抽样必须落在 [O, P] 内
    rng = random.Random(7)
    for _, o, m, p, _ in EPICS:
        for _ in range(200):
            v = sample_pert(rng, o, m, p)
            if not (o - 1e-9 <= v <= p + 1e-9):
                print(f"[SELFTEST FAIL] 抽样 {v} 越界 [{o}, {p}]")
                failures += 1
                break

    # 5. 退化情形：O == M == P 时结果恒定
    if abs(sample_pert(random.Random(3), 5, 5, 5) - 5) > 1e-9:
        print("[SELFTEST FAIL] O==M==P 时抽样应恒为该值")
        failures += 1

    # 6. 结果可复现（同 seed 同结果）
    if simulate(trials=2000, seed=42) != simulate(trials=2000, seed=42):
        print("[SELFTEST FAIL] 同 seed 的模拟结果应可复现")
        failures += 1

    print("self-test: " + ("PASS" if failures == 0 else f"FAIL ({failures})"))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trials", type=int, default=100000)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    return self_test() if args.self_test else report(args.trials)


if __name__ == "__main__":
    raise SystemExit(main())
