#!/usr/bin/env python3
"""状态机定义的**契约自洽性检查**（schema/sandbox-state-machine.json）。

⚠️ 本脚本**不构成对系统行为的任何验证**。它检查的是「这份 JSON 是否符合 04 文档写下的规则」。

具体强度分两类，使用者必须区分：

- **图结构不变式（INV-0/2/3/4/5）**：真正可判定的性质，如可达性、终态无出边、
  「不存在无法回收的沙盒」。这类检查有实质价值。
- **标签约定检查（INV-1/6）**：只能检查字符串标签是否符合约定，
  例如 INV-6 检查的是转换的 trigger 里**有没有出现**某几个字——
  **这是在检查标签，不是在检查行为**。实现是否真的做了对应动作，本脚本无从得知。

这份脚本的价值在于：状态机是后续 Airflow DAG、API 状态校验、回收巡检的共同来源，
一旦有人新增状态或转换，图结构类检查能立刻发现「出现了无法回收的沙盒」这类严重缺陷。

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
        violations.append("INV-1 pending_review 必须为只读，否则审批内容与实际导出内容可能不一致")

    # INV-2 exporting 不可被丢弃打断（图结构不变式）
    if any(tr["from"] == "exporting" and tr["to"] == "discarding" for tr in transitions):
        violations.append("INV-2 exporting 状态不允许直接转入 discarding")

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

    # INV-6 【标签约定检查，非行为验证】导出失败的 trigger 必须声明未写生产库。
    # 首版设计上根本不写生产库（05 文档 1.2），因此这条只是防止有人在不做决策变更的情况下
    # 悄悄把写生产的语义塞回状态机。它检查的是字符串标签，实现是否真的没写生产库本脚本无从得知。
    for tr in transitions:
        if tr["from"] == "exporting" and tr["to"] == "failed":
            if "no_production_write" not in tr.get("trigger", ""):
                violations.append(
                    "INV-6【标签检查】exporting->failed 的 trigger 应声明未写生产库，实际为 %s" % tr.get("trigger")
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
