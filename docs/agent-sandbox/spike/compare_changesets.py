#!/usr/bin/env python3
"""比对两条采集路径（块级差异 / 逻辑日志）产出的 ChangeSet 是否语义等价。

规范化掉两条路径天然不同、且不影响合并语义的字段（行顺序、txn_id/position/occurred_at、
changed_columns 顺序、checksum 等），然后对以下内容做严格比对：

  - 表集合与每表 insert/update/delete 计数
  - 每一行的 op / pk / before / after / changed_columns
  - ddl_changes 的 ddl_type / object / risk
  - integrity.completeness

结论分三类，其中 MISSING（漏报）一旦出现即判定该路径不可用。

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
    return (table, row["op"], canonical_value(row.get("pk")))


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
            key = row_key(table, row)
            if key in index:
                # 同一 (表, op, pk) 出现多次（例如保留操作序列的表示），用序号区分
                seq = 2
                while (key + (seq,)) in index:
                    seq += 1
                key = key + (seq,)
            index[key] = normalise_row(row)
    return index


def index_ddl(doc: dict[str, Any]) -> dict[tuple, dict[str, Any]]:
    return {
        (d["ddl_type"], d["object"]): {"risk": d.get("risk"), "reversible": d.get("reversible")}
        for d in doc.get("ddl_changes", [])
    }


def compare(a: dict[str, Any], b: dict[str, Any], label_a: str = "A", label_b: str = "B") -> list[str]:
    findings: list[str] = []

    # 1. integrity.completeness
    ca = a.get("integrity", {}).get("completeness")
    cb = b.get("integrity", {}).get("completeness")
    if ca != cb:
        findings.append(f"MISMATCH integrity.completeness: {label_a}={ca} {label_b}={cb}")

    # 2. 表集合与计数
    counts_a = {tc["table"]: tc.get("counts", {}) for tc in a.get("table_changes", [])}
    counts_b = {tc["table"]: tc.get("counts", {}) for tc in b.get("table_changes", [])}
    for table in sorted(set(counts_a) - set(counts_b)):
        findings.append(f"MISSING 表 {table} 只出现在 {label_a}，{label_b} 漏报整张表")
    for table in sorted(set(counts_b) - set(counts_a)):
        findings.append(f"MISSING 表 {table} 只出现在 {label_b}，{label_a} 漏报整张表")
    for table in sorted(set(counts_a) & set(counts_b)):
        if counts_a[table] != counts_b[table]:
            findings.append(
                f"MISMATCH 表 {table} 计数不一致：{label_a}={counts_a[table]} {label_b}={counts_b[table]}"
            )

    # 3. 行级明细
    rows_a, rows_b = index_rows(a), index_rows(b)
    for key in sorted(set(rows_a) - set(rows_b), key=str):
        findings.append(f"MISSING 行 {key} 只出现在 {label_a}，{label_b} 漏报")
    for key in sorted(set(rows_b) - set(rows_a), key=str):
        findings.append(f"MISSING 行 {key} 只出现在 {label_b}，{label_a} 漏报")
    for key in sorted(set(rows_a) & set(rows_b), key=str):
        if rows_a[key] != rows_b[key]:
            diff_fields = sorted(
                f for f in set(rows_a[key]) | set(rows_b[key]) if rows_a[key].get(f) != rows_b[key].get(f)
            )
            findings.append(f"MISMATCH 行 {key} 字段不一致：{diff_fields}")

    # 4. DDL
    ddl_a, ddl_b = index_ddl(a), index_ddl(b)
    for key in sorted(set(ddl_a) - set(ddl_b), key=str):
        findings.append(f"MISSING DDL {key} 只出现在 {label_a}")
    for key in sorted(set(ddl_b) - set(ddl_a), key=str):
        findings.append(f"MISSING DDL {key} 只出现在 {label_b}")
    for key in sorted(set(ddl_a) & set(ddl_b), key=str):
        if ddl_a[key] != ddl_b[key]:
            findings.append(f"MISMATCH DDL {key}：{label_a}={ddl_a[key]} {label_b}={ddl_b[key]}")

    return findings


def report(findings: list[str], label_a: str, label_b: str) -> int:
    missing = [f for f in findings if f.startswith("MISSING")]
    if not findings:
        print(f"EQUIVALENT · {label_a} 与 {label_b} 语义等价")
        return 0
    print(f"NOT EQUIVALENT · 共 {len(findings)} 项差异，其中漏报 {len(missing)} 项")
    for finding in findings:
        print("  - " + finding)
    if missing:
        print("\n结论：存在漏报，该路径直接判负（对应 POC 红线『差异一致率 = 100%』）。")
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
    if not any(f.startswith("MISSING") for f in findings):
        print("[SELFTEST FAIL] 注入漏报后本应检出 MISSING")
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

    # 负样本 4：completeness 不一致
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
