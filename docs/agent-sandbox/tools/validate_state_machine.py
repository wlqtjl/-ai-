#!/usr/bin/env python3
"""校验沙盒状态机定义（schema/sandbox-state-machine.json）满足 04 文档声明的全部不变式。

这份脚本的价值在于：状态机是后续 Airflow DAG、API 状态校验、回收巡检的共同来源，
一旦有人新增状态或转换，这里能立刻发现「出现了无法回收的沙盒」这类严重缺陷。

用法：
    python3 tools/validate_state_machine.py
退出码：0 全部不变式通过；1 存在违反。
"""

from __future__ import annotations

import json
import os
import sys
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_PATH = os.path.join(HERE, "..", "schema", "sandbox-state-machine.json")

EXPECTED_RECYCLE_ORDER = [
    "drop_temp_credentials",
    "unmount",
    "delete_mount_task",
    "delete_physical_copy",
    "release_quota_and_audit",
]


def load_spec() -> dict:
    with open(SPEC_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def reachable_from(spec: dict, start: str) -> set[str]:
    edges: dict[str, list[str]] = {}
    for tr in spec["transitions"]:
        edges.setdefault(tr["from"], []).append(tr["to"])
    seen = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in edges.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def check(spec: dict) -> list[str]:
    violations: list[str] = []
    states = spec["states"]
    transitions = spec["transitions"]
    terminal = set(spec["terminal_states"])

    # 转换引用的状态必须存在
    for tr in transitions:
        for side in ("from", "to"):
            if tr[side] not in states:
                violations.append(f"转换 {tr} 引用了未定义状态 {tr[side]}")

    # 所有状态从初始态可达
    reachable = reachable_from(spec, spec["initial_state"])
    for state in states:
        if state not in reachable:
            violations.append(f"INV-0 状态 {state} 从 {spec['initial_state']} 不可达（死状态）")

    # INV-1 pending_review 只读
    if states.get("pending_review", {}).get("connectable") != "readonly":
        violations.append("INV-1 pending_review 必须为只读，否则审批内容与实际合并内容可能不一致")

    # INV-2 merging 不可被丢弃打断
    if any(tr["from"] == "merging" and tr["to"] == "discarding" for tr in transitions):
        violations.append("INV-2 merging 状态不允许直接转入 discarding")

    # INV-3 discarded 无出边
    if any(tr["from"] == "discarded" for tr in transitions):
        violations.append("INV-3 discarded 是终态，不应有出边")

    # INV-4 所有非终态都能到达 discarded
    for state in states:
        if state == "discarded":
            continue
        if "discarded" not in reachable_from(spec, state):
            violations.append(f"INV-4 状态 {state} 无法到达 discarded，存在无法回收的沙盒")

    # INV-5 只有 discarded 不占配额
    free = {name for name, meta in states.items() if not meta.get("holds_quota")}
    if free != {"discarded"}:
        violations.append(f"INV-5 不占配额的状态集合应为 {{discarded}}，实际为 {free or '{}'}")

    # INV-6 合并失败必须已回退生产库
    for tr in transitions:
        if tr["from"] == "merging" and tr["to"] == "failed":
            if "production_rolled_back" not in tr.get("trigger", ""):
                violations.append(
                    "INV-6 merging->failed 的 trigger 必须体现生产库已回退，实际为 %s" % tr.get("trigger")
                )

    # 回收顺序：凭据必须最先回收，物理副本必须晚于挂载任务删除
    order = spec.get("recycle_order", [])
    if order != EXPECTED_RECYCLE_ORDER:
        violations.append(
            "回收顺序与 04 文档第 4 节不一致：期望 %s，实际 %s" % (EXPECTED_RECYCLE_ORDER, order)
        )

    return violations


def main() -> int:
    spec = load_spec()
    violations = check(spec)
    print(
        "状态机：%d 个状态 / %d 条转换 / 初始态 %s"
        % (len(spec["states"]), len(spec["transitions"]), spec["initial_state"])
    )
    if violations:
        print("[FAIL] 违反 %d 条不变式：" % len(violations))
        for v in violations:
            print("  - " + v)
        return 1
    print("[ OK ] 全部不变式通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
