#!/usr/bin/env python3
"""POC 验收判定器。

把 08-poc-acceptance.md 的指标表变成可执行的检查项，避免用「客户觉得不错」结题。

⚠️ 本脚本判定的是「填进来的数字是否满足冻结的阈值」，它**不验证数字本身的真实性**。
证据来源见指标表的 evidence 字段。

【第二轮修订】删除了不可证明的指标。典型如第一轮的「差异一致率 = 100%」：
抽样在数学上永远无法证明总体为 100%，第一轮却把它做成 `== 1.0` 的浮点相等判定，
让一个不可验证的命题看起来像被自动化验证了——这是把不严谨包装成了严谨。
现改为两个可证明的指标：
  1. fixture 全集上的失配数（全量可判定，红线 = 0）；
  2. 生产负载下的失配率**上界**（用观测数与样本量给出 95% 置信上界，而不是宣称 100%）。

判定规则：
  - redline（红线）：任一不达标 -> POC 判负（退出码 1）
  - target（目标）：不达标记为待改进（退出码 3）
  - 指标缺失（null）：视为未达标，档位按其定义处理

用法：
    python3 tools/poc_gate.py <metrics.json>
    python3 tools/poc_gate.py --self-test
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from typing import Any, Callable

Check = tuple[str, str, str, Callable[[Any], bool], str]

# (指标键, 档位, 中文名, 判定函数, 目标描述)
CHECKS: list[Check] = [
    ("source_side_additions", "redline", "源端新增权限/挂载/代理数量", lambda v: v == 0, "= 0"),
    ("fixture_mismatch_count", "redline", "fixture 全集失配数", lambda v: v == 0, "= 0（全量可判定）"),
    ("unexpected_production_writes", "redline", "非预期的生产库写入次数", lambda v: v == 0, "= 0"),
    ("sla_targets_derived_from_measurement", "redline", "SLA 目标是否由实测反推", lambda v: v is True, "= true"),
    ("agent_self_approval_count", "redline", "Agent 自审自批次数", lambda v: v == 0, "= 0"),
    ("unreclaimed_sandbox_count", "redline", "到期未回收沙盒数", lambda v: v == 0, "= 0"),
    ("create_near_current_p95_seconds", "target", "near_current 创建 P95", lambda v: v < 300, "< 300 秒"),
    ("end_to_end_time_reduction_pct", "target", "端到端耗时降幅", lambda v: v >= 80, "≥ 80%"),
    ("max_concurrent_sandboxes", "target", "并发存活沙盒上限", lambda v: v >= 100, "≥ 100"),
    ("estimate_error_ratio", "target", "创建耗时预估误差", lambda v: v <= 0.5, "≤ 0.5"),
    ("export_success_rate", "target", "变更集导出成功率", lambda v: v >= 0.99, "≥ 99%"),
    ("production_workload_mismatch_upper_bound", "target", "生产负载失配率 95% 上界", lambda v: v <= 0.001, "≤ 0.1%"),
    ("backup_task_degradation_pct", "target", "备份任务耗时劣化", lambda v: v <= 5, "≤ 5%"),
    ("storage_amplification_per_gb_written", "target", "每 GB 写入的存储放大倍数", lambda v: v <= 2.0, "≤ 2.0 倍"),
    ("masking_enabled_rate", "redline", "脱敏默认开启比例", lambda v: v == 1.0, "= 100%"),
]


def mismatch_rate_upper_bound(mismatches: int, sample_size: int) -> float | None:
    """生产负载失配率的 95% 置信上界。

    这是替代「一致率 = 100%」的可证明表述：抽样无法证明总体为 0，
    但可以给出「在 95% 置信度下，失配率不超过 X」。

    - 观测到 0 次失配时用 rule of three：上界 ≈ 3 / n；
    - 观测到 k > 0 次时用 Wilson 区间上界。
    """
    if not sample_size or sample_size <= 0 or mismatches is None:
        return None
    if mismatches == 0:
        return 3.0 / sample_size
    z = 1.96
    n = float(sample_size)
    phat = mismatches / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * ((phat * (1 - phat) / n + z * z / (4 * n * n)) ** 0.5)
    return (centre + margin) / denom


def derive_metrics(doc: dict[str, Any]) -> None:
    """从原始观测量推导可证明的指标，避免让人直接手填一个「率」。"""
    metrics = doc.setdefault("metrics", {})
    observed = doc.get("observations", {})
    if metrics.get("production_workload_mismatch_upper_bound") is None:
        bound = mismatch_rate_upper_bound(
            observed.get("production_workload_mismatches"),
            observed.get("production_workload_rows_compared"),
        )
        if bound is not None:
            metrics["production_workload_mismatch_upper_bound"] = round(bound, 6)


def evaluate(doc: dict[str, Any]) -> tuple[list[str], list[str]]:
    derive_metrics(doc)
    metrics = doc.get("metrics", {})
    redline_failures: list[str] = []
    target_failures: list[str] = []
    for key, tier, name, predicate, want in CHECKS:
        value = metrics.get(key)
        if value is None:
            message = f"{name}（{key}）未填写，视为未达标（要求 {want}）"
            ok = False
        else:
            ok = bool(predicate(value))
            message = f"{name}（{key}）实测 {value}，要求 {want}"
        if not ok:
            (redline_failures if tier == "redline" else target_failures).append(message)
    return redline_failures, target_failures


def report(doc: dict[str, Any]) -> int:
    derive_metrics(doc)
    metrics = doc.get("metrics", {})
    print(f"POC 运行：{doc.get('run_id')}  客户：{doc.get('customer') or '(未填)'}  库类型：{doc.get('db_type')}")
    for key, tier, name, predicate, want in CHECKS:
        value = metrics.get(key)
        if value is None:
            status = "缺失"
        else:
            status = "PASS" if predicate(value) else "FAIL"
        tag = "红线" if tier == "redline" else "目标"
        print(f"  [{tag}] {name:<28} {str(value):<10} 要求 {want:<10} {status}")

    redline, target = evaluate(doc)
    print()
    if redline:
        print(f"结论：POC 判负 —— {len(redline)} 项红线指标未达标")
        for item in redline:
            print("  ✗ " + item)
        if target:
            print(f"  （另有 {len(target)} 项目标指标未达标）")
        return 1
    if target:
        print(f"结论：有条件通过 —— 红线全部达标，{len(target)} 项目标指标待改进")
        for item in target:
            print("  ! " + item)
        return 3
    print("结论：通过 —— 全部红线与目标指标达标")
    return 0


def _passing_doc() -> dict[str, Any]:
    return {
        "run_id": "selftest",
        "customer": "demo",
        "db_type": "mysql",
        "metrics": {
            "source_side_additions": 0,
            "fixture_mismatch_count": 0,
            "unexpected_production_writes": 0,
            "sla_targets_derived_from_measurement": True,
            "agent_self_approval_count": 0,
            "unreclaimed_sandbox_count": 0,
            "masking_enabled_rate": 1.0,
            "create_near_current_p95_seconds": 210,
            "end_to_end_time_reduction_pct": 92,
            "max_concurrent_sandboxes": 120,
            "estimate_error_ratio": 0.3,
            "export_success_rate": 0.995,
            "production_workload_mismatch_upper_bound": None,
            "backup_task_degradation_pct": 2,
            "storage_amplification_per_gb_written": 1.4,
        },
        "observations": {
            "production_workload_mismatches": 0,
            "production_workload_rows_compared": 50000,
        },
    }


def self_test() -> int:
    failures = 0

    ok_doc = _passing_doc()
    redline, target = evaluate(ok_doc)
    if redline or target:
        print(f"[SELFTEST FAIL] 全达标样本本应无失败项，实际 redline={redline} target={target}")
        failures += 1

    # 红线失败样本：源端被动了一处
    bad = copy.deepcopy(ok_doc)
    bad["metrics"]["source_side_additions"] = 1
    redline, _ = evaluate(bad)
    if len(redline) != 1:
        print("[SELFTEST FAIL] 源端新增 1 处本应触发且仅触发 1 项红线失败")
        failures += 1

    # 红线失败样本：fixture 全集只要有 1 条失配即判负（这是全量可判定的，不是抽样）
    bad = copy.deepcopy(ok_doc)
    bad["metrics"]["fixture_mismatch_count"] = 1
    if not evaluate(bad)[0]:
        print("[SELFTEST FAIL] fixture 出现失配本应判定红线失败")
        failures += 1

    # 红线失败样本：SLA 目标不是由实测反推的（矛盾 3 的制度化防线）
    bad = copy.deepcopy(ok_doc)
    bad["metrics"]["sla_targets_derived_from_measurement"] = False
    if not evaluate(bad)[0]:
        print("[SELFTEST FAIL] SLA 目标未由实测反推本应判定红线失败")
        failures += 1

    # 失配率上界：0/50000 走 rule of three，应约为 6e-5，且随样本量增大而收紧
    bound_small = mismatch_rate_upper_bound(0, 1000)
    bound_large = mismatch_rate_upper_bound(0, 50000)
    if not (bound_large < bound_small and abs(bound_large - 3 / 50000) < 1e-12):
        print(f"[SELFTEST FAIL] rule of three 上界计算有误：{bound_small} / {bound_large}")
        failures += 1
    if mismatch_rate_upper_bound(5, 1000) is None or mismatch_rate_upper_bound(5, 1000) <= 5 / 1000:
        print("[SELFTEST FAIL] 观测到失配时 Wilson 上界应大于点估计")
        failures += 1

    # 样本量不足时不得凭空推导出一个「上界」
    if mismatch_rate_upper_bound(0, 0) is not None:
        print("[SELFTEST FAIL] 样本量为 0 时不应给出上界")
        failures += 1

    # 推导必须真的发生：清空该指标后应由 observations 推出
    derived = copy.deepcopy(ok_doc)
    derived["metrics"]["production_workload_mismatch_upper_bound"] = None
    evaluate(derived)
    if derived["metrics"]["production_workload_mismatch_upper_bound"] is None:
        print("[SELFTEST FAIL] 失配率上界本应由 observations 推导得出")
        failures += 1

    # 目标未达样本
    warn = copy.deepcopy(ok_doc)
    warn["metrics"]["max_concurrent_sandboxes"] = 40
    redline, target = evaluate(warn)
    if redline or len(target) != 1:
        print("[SELFTEST FAIL] 并发未达标本应只触发目标未达")
        failures += 1

    # 缺失值样本
    missing = copy.deepcopy(ok_doc)
    missing["metrics"]["agent_self_approval_count"] = None
    if not evaluate(missing)[0]:
        print("[SELFTEST FAIL] 红线指标缺失本应视为未达标")
        failures += 1

    # 模板必须包含全部指标键
    here = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.join(here, "..", "templates", "poc-metrics.template.json")
    with open(template_path, encoding="utf-8") as fh:
        template = json.load(fh)
    missing_keys = [key for key, *_ in CHECKS if key not in template.get("metrics", {})]
    if missing_keys:
        print(f"[SELFTEST FAIL] 模板缺少指标键：{missing_keys}")
        failures += 1

    print("self-test: " + ("PASS" if failures == 0 else f"FAIL ({failures})"))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("metrics_file", nargs="?", help="填写好实测值的指标 JSON")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.metrics_file:
        parser.error("请提供指标文件，或使用 --self-test")
    with open(args.metrics_file, encoding="utf-8") as fh:
        return report(json.load(fh))


if __name__ == "__main__":
    sys.exit(main())
