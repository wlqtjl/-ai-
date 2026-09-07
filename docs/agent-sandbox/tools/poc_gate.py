#!/usr/bin/env python3
"""POC 验收判定器。

把 08-poc-acceptance.md 的指标表变成可执行的检查项，避免用「客户觉得不错」结题。

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
    ("changeset_consistency_rate", "redline", "ChangeSet 与实际变更一致率", lambda v: v == 1.0, "= 100%"),
    ("production_impacted_on_failure_count", "redline", "失败时生产库受影响次数", lambda v: v == 0, "= 0"),
    ("agent_self_approval_count", "redline", "Agent 自审自批次数", lambda v: v == 0, "= 0"),
    ("unreclaimed_sandbox_count", "redline", "到期未回收沙盒数", lambda v: v == 0, "= 0"),
    ("create_near_current_p95_seconds", "target", "near_current 创建 P95", lambda v: v < 300, "< 300 秒"),
    ("end_to_end_time_reduction_pct", "target", "端到端耗时降幅", lambda v: v >= 80, "≥ 80%"),
    ("max_concurrent_sandboxes", "target", "并发存活沙盒上限", lambda v: v >= 100, "≥ 100"),
    ("estimate_error_ratio", "target", "创建耗时预估误差", lambda v: v <= 0.5, "≤ 0.5"),
    ("merge_success_rate", "target", "合并回切成功率", lambda v: v >= 0.99, "≥ 99%"),
    ("backup_task_degradation_pct", "target", "备份任务耗时劣化", lambda v: v <= 5, "≤ 5%"),
    ("storage_ratio_vs_full_clone", "target", "单沙盒存储/全量克隆", lambda v: v <= 0.05, "≤ 5%"),
    ("masking_enabled_rate", "target", "脱敏默认开启比例", lambda v: v == 1.0, "= 100%"),
]


def evaluate(doc: dict[str, Any]) -> tuple[list[str], list[str]]:
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
            "changeset_consistency_rate": 1.0,
            "production_impacted_on_failure_count": 0,
            "agent_self_approval_count": 0,
            "unreclaimed_sandbox_count": 0,
            "create_near_current_p95_seconds": 210,
            "end_to_end_time_reduction_pct": 92,
            "max_concurrent_sandboxes": 120,
            "estimate_error_ratio": 0.3,
            "merge_success_rate": 0.995,
            "backup_task_degradation_pct": 2,
            "storage_ratio_vs_full_clone": 0.03,
            "masking_enabled_rate": 1.0,
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

    # 红线失败样本：差异一致率 99.9% 也不合格
    bad = copy.deepcopy(ok_doc)
    bad["metrics"]["changeset_consistency_rate"] = 0.999
    if not evaluate(bad)[0]:
        print("[SELFTEST FAIL] 一致率 99.9% 本应判定红线失败（漏报不可接受）")
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
