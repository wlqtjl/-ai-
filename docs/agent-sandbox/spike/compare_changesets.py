#!/usr/bin/env python3
"""比对两条采集路径（块级差异 / 逻辑日志）产出的 ChangeSet 净状态差是否等价。

⚠️ 本脚本是**契约自洽性检查**工具，不构成对采集实现正确性的验证。

前提（01 文档第 3 节）：ChangeSet 的规范形式是**净状态差**，不是操作序列。
因此比对前先检查净差不变式（同表内主键唯一），不满足则拒绝比对——
两份语义类型不同的文档强行比对，只会产生无意义的差异清单。
第一轮正是缺了这一步，把「语义类型不匹配」误报成了「漏报」。

规范化掉两条路径天然不同、且不影响语义的字段（行顺序、txn_id/position/occurred_at、
changed_columns 顺序、checksum 等），然后对以下内容做严格比对：

  - 表集合与每表 insert/update/delete 计数
  - 每一行的 op / pk / before / after / changed_columns（按 表+主键 配对）
  - ddl_changes 的 ddl_type / object / risk
  - integrity.completeness

结论：ONLY_IN_<label>（某侧缺失，等同漏报，最严重）、MISMATCH（值不一致）、EQUIVALENT。

用法：
    python3 compare_changesets.py a.json b.json
    python3 compare_changesets.py --self-test
退出码：0 等价；1 存在差异；2 参数错误。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from typing import Any

IGNORED_ROW_FIELDS = ("txn_id", "position", "occurred_at")


def canonical_value(value: Any) -> Any:
    """把值规范化为可比较、可哈希的形式（数值统一按字符串化的十进制处理）。"""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return tuple(sorted((k, canonical_value(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(canonical_value(v) for v in value)
    return value


def row_key(table: str, row: dict[str, Any]) -> tuple:
    """净状态差下每个 (表, 主键) 最多一条记录，因此 op 不进 key。

    第一轮把 op 放进了 key，导致「同一行两侧 op 不同」被报成两条『漏报』，
    而它实际上是一条值不一致。
    """
    return (table, canonical_value(row.get("pk")))


def net_diff_violations(doc: dict[str, Any], label: str) -> list[str]:
    """净差不变式：同一表内主键唯一。违反说明该侧未折叠为净差。"""
    violations: list[str] = []
    for tc in doc.get("table_changes", []):
        seen: dict[str, int] = {}
        for row in tc.get("rows", []):
            key = json.dumps(canonical_value(row.get("pk")), sort_keys=True, ensure_ascii=False, default=str)
            seen[key] = seen.get(key, 0) + 1
        for key, count in sorted(seen.items()):
            if count > 1:
                violations.append(
                    "%s 的表 %s 主键 %s 出现 %d 次：该侧仍是操作序列，未折叠为净状态差"
                    % (label, tc.get("table"), key, count)
                )
    return violations


def normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k not in IGNORED_ROW_FIELDS}
    out["before"] = canonical_value(row.get("before"))
    out["after"] = canonical_value(row.get("after"))
    out["changed_columns"] = tuple(sorted(row.get("changed_columns") or []))
    return out


def index_rows(doc: dict[str, Any]) -> dict[tuple, dict[str, Any]]:
    index: dict[tuple, dict[str, Any]] = {}
    for tc in doc.get("table_changes", []):
        table = tc["table"]
        for row in tc.get("rows", []):
            index[row_key(table, row)] = normalise_row(row)
    return index


def index_ddl(doc: dict[str, Any]) -> dict[tuple, dict[str, Any]]:
    return {
        (d["ddl_type"], d["object"]): {"risk": d.get("risk"), "reversible": d.get("reversible")}
        for d in doc.get("ddl_changes", [])
    }


def compare(a: dict[str, Any], b: dict[str, Any], label_a: str = "A", label_b: str = "B") -> list[str]:
    findings: list[str] = []

    # 0. 前置：两侧都必须已折叠为净状态差，否则比对结果没有意义
    precondition = net_diff_violations(a, label_a) + net_diff_violations(b, label_b)
    if precondition:
        return ["PRECONDITION " + v for v in precondition]

    # 1. integrity.completeness
    ca = a.get("integrity", {}).get("completeness")
    cb = b.get("integrity", {}).get("completeness")
    if ca != cb:
        findings.append(f"MISMATCH integrity.completeness: {label_a}={ca} {label_b}={cb}")

    # 2. 表集合与计数
    counts_a = {tc["table"]: tc.get("counts", {}) for tc in a.get("table_changes", [])}
    counts_b = {tc["table"]: tc.get("counts", {}) for tc in b.get("table_changes", [])}
    for table in sorted(set(counts_a) - set(counts_b)):
        findings.append(f"ONLY_IN_{label_a} 表 {table} 只出现在 {label_a}，{label_b} 缺失整张表")
    for table in sorted(set(counts_b) - set(counts_a)):
        findings.append(f"ONLY_IN_{label_b} 表 {table} 只出现在 {label_b}，{label_a} 缺失整张表")
    for table in sorted(set(counts_a) & set(counts_b)):
        if counts_a[table] != counts_b[table]:
            findings.append(
                f"MISMATCH 表 {table} 计数不一致：{label_a}={counts_a[table]} {label_b}={counts_b[table]}"
            )

    # 3. 行级明细
    rows_a, rows_b = index_rows(a), index_rows(b)
    for key in sorted(set(rows_a) - set(rows_b), key=str):
        findings.append(f"ONLY_IN_{label_a} 行 {key} 只出现在 {label_a}，{label_b} 缺失")
    for key in sorted(set(rows_b) - set(rows_a), key=str):
        findings.append(f"ONLY_IN_{label_b} 行 {key} 只出现在 {label_b}，{label_a} 缺失")
    for key in sorted(set(rows_a) & set(rows_b), key=str):
        if rows_a[key] != rows_b[key]:
            diff_fields = sorted(
                f for f in set(rows_a[key]) | set(rows_b[key]) if rows_a[key].get(f) != rows_b[key].get(f)
            )
            findings.append(f"MISMATCH 行 {key} 字段不一致：{diff_fields}")

    # 4. DDL
    ddl_a, ddl_b = index_ddl(a), index_ddl(b)
    for key in sorted(set(ddl_a) - set(ddl_b), key=str):
        findings.append(f"ONLY_IN_{label_a} DDL {key} 只出现在 {label_a}")
    for key in sorted(set(ddl_b) - set(ddl_a), key=str):
        findings.append(f"ONLY_IN_{label_b} DDL {key} 只出现在 {label_b}")
    for key in sorted(set(ddl_a) & set(ddl_b), key=str):
        if ddl_a[key] != ddl_b[key]:
            findings.append(f"MISMATCH DDL {key}：{label_a}={ddl_a[key]} {label_b}={ddl_b[key]}")

    return findings


def report(findings: list[str], label_a: str, label_b: str) -> int:
    if not findings:
        print(f"EQUIVALENT · {label_a} 与 {label_b} 的净状态差一致")
        return 0
    precondition = [f for f in findings if f.startswith("PRECONDITION")]
    if precondition:
        print("PRECONDITION FAILED · 输入未满足净状态差规范，比对无意义：")
        for finding in precondition:
            print("  - " + finding)
        print("\n处理：先在采集侧完成折叠（01 文档第 3.3 节折叠规则），再重跑比对。")
        return 1
    missing = [f for f in findings if f.startswith("ONLY_IN_")]
    print(f"NOT EQUIVALENT · 共 {len(findings)} 项差异，其中单侧缺失 {len(missing)} 项")
    for finding in findings:
        print("  - " + finding)
    if missing:
        print("\n结论：存在单侧缺失（等同漏报），该路径直接判负。")
        print("对应指标口径见 08 文档：fixture 全集零失配（本项）+ 生产负载失配率上界。")
    return 1


def self_test() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    examples = os.path.join(here, "..", "examples")
    with open(os.path.join(examples, "changeset.mysql.example.json"), encoding="utf-8") as fh:
        a = json.load(fh)
    with open(os.path.join(examples, "changeset.block-diff.example.json"), encoding="utf-8") as fh:
        b = json.load(fh)

    failures = 0

    # 正样本：两条路径应等价
    findings = compare(a, b, "logical_log", "block_diff")
    if findings:
        print("[SELFTEST FAIL] 正样本本应等价，却报出差异：")
        for f in findings:
            print("    " + f)
        failures += 1

    # 负样本 1：注入漏报（B 少一行 delete）
    b_missing = copy.deepcopy(b)
    b_missing["table_changes"][0]["rows"] = [
        r for r in b_missing["table_changes"][0]["rows"] if r["op"] != "delete"
    ]
    b_missing["table_changes"][0]["counts"]["delete"] = 0
    findings = compare(a, b_missing)
    if not any(f.startswith("ONLY_IN_") for f in findings):
        print("[SELFTEST FAIL] 注入缺失后本应检出 ONLY_IN_*")
        failures += 1

    # 负样本 2：注入值不一致（B 的 after 值被改）
    b_wrong = copy.deepcopy(b)
    for row in b_wrong["table_changes"][0]["rows"]:
        if row["op"] == "update":
            row["after"]["amount"] = 999.0
    findings = compare(a, b_wrong)
    if not any(f.startswith("MISMATCH 行") for f in findings):
        print("[SELFTEST FAIL] 注入值差异后本应检出 MISMATCH")
        failures += 1

    # 负样本 3：注入 DDL 漏报
    b_no_ddl = copy.deepcopy(b)
    b_no_ddl["ddl_changes"] = []
    findings = compare(a, b_no_ddl)
    if not any("DDL" in f for f in findings):
        print("[SELFTEST FAIL] DDL 漏报本应被检出")
        failures += 1

    # 负样本 4：未折叠的操作序列必须在前置检查阶段被拒绝，而不是被误报成漏报
    b_unfolded = copy.deepcopy(b)
    first = b_unfolded["table_changes"][0]
    dup_row = copy.deepcopy(first["rows"][0])
    first["rows"].append(dup_row)
    findings = compare(a, b_unfolded)
    if not findings or not all(f.startswith("PRECONDITION") for f in findings):
        print("[SELFTEST FAIL] 未折叠为净差的输入本应触发 PRECONDITION，而非逐条差异")
        failures += 1

    # 负样本 5：仅溯源字段不同时必须判为等价
    b_trace = copy.deepcopy(b)
    for tc in b_trace["table_changes"]:
        for row in tc["rows"]:
            row["position"] = "irrelevant:1"
            row["txn_id"] = "0xdeadbeef"
    if compare(a, b_trace):
        print("[SELFTEST FAIL] 仅溯源字段不同本应判为等价")
        failures += 1

    # 负样本 6：同一行两侧 op 不同，应报 1 条 MISMATCH 而非 2 条单侧缺失
    b_op = copy.deepcopy(b)
    for tc in b_op["table_changes"]:
        for row in tc["rows"]:
            if row["op"] == "delete":
                row["op"] = "update"
                row["after"] = {"x": 1}
                break
    findings = compare(a, b_op)
    if any(f.startswith("ONLY_IN_") and "行" in f for f in findings):
        print("[SELFTEST FAIL] op 不同应报 MISMATCH，不应报单侧缺失")
        failures += 1

    # 负样本 7：completeness 不一致
    b_trunc = copy.deepcopy(b)
    b_trunc["integrity"]["completeness"] = "truncated"
    if not any("completeness" in f for f in compare(a, b_trunc)):
        print("[SELFTEST FAIL] completeness 差异本应被检出")
        failures += 1

    print("self-test: " + ("PASS" if failures == 0 else f"FAIL ({failures})"))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="两份待比对的 ChangeSet JSON 文件")
    parser.add_argument("--self-test", action="store_true", help="用内置正/负样本做回归自测")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if len(args.files) != 2:
        parser.error("需要恰好两个文件，或使用 --self-test")

    docs = []
    for path in args.files:
        with open(path, encoding="utf-8") as fh:
            docs.append(json.load(fh))
    label_a = docs[0].get("capture", {}).get("path", os.path.basename(args.files[0]))
    label_b = docs[1].get("capture", {}).get("path", os.path.basename(args.files[1]))
    return report(compare(docs[0], docs[1], label_a, label_b), label_a, label_b)


if __name__ == "__main__":
    sys.exit(main())
