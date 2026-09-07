#!/usr/bin/env python3
"""ChangeSet 校验器。

两层校验：
1. 结构校验：对 schema/changeset.schema.json 做 JSON Schema 验证（缺少 jsonschema 依赖时降级为内建的最小结构检查）。
2. 门禁校验（gate）：实现 01-changeset-model.md 中的硬规则，判断该 ChangeSet 是否允许进入审批/合并流程。

用法：
    python3 tools/validate_changeset.py examples/*.json
    python3 tools/validate_changeset.py --self-test
退出码：0 全部通过；1 存在结构错误；2 结构正确但存在被门禁拒绝的变更集（属于预期内的业务结论，见 --allow-blocked）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "..", "schema", "changeset.schema.json")


def load_schema() -> dict[str, Any]:
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def structural_errors(doc: Any, schema: dict[str, Any]) -> list[str]:
    """优先使用 jsonschema；不可用时退化为最小必填字段检查。"""
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return _fallback_structural_errors(doc)

    validator = jsonschema.Draft7Validator(schema)
    return [
        "/" + "/".join(str(p) for p in err.absolute_path) + ": " + err.message
        for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    ]


def _fallback_structural_errors(doc: Any) -> list[str]:
    required = [
        "schema_version",
        "changeset_id",
        "sandbox_id",
        "capture",
        "baseline",
        "summary",
        "ddl_changes",
        "table_changes",
        "conflicts",
        "integrity",
    ]
    if not isinstance(doc, dict):
        return ["root: expected object"]
    return [f"/{key}: missing required field" for key in required if key not in doc]


# --- 门禁规则 -------------------------------------------------------------


def gate(doc: dict[str, Any]) -> list[str]:
    """返回拒绝理由列表；空列表表示允许进入审批流程。"""
    reasons: list[str] = []
    integrity = doc.get("integrity", {})

    if integrity.get("completeness") != "complete":
        reasons.append(
            "integrity.completeness=%s，差异可能漏报，禁止进入审批与合并流程"
            % integrity.get("completeness")
        )

    if doc.get("conflicts"):
        reasons.append("存在 %d 条冲突，首版策略为冲突即拒绝，需基于最新时间点重建沙盒" % len(doc["conflicts"]))

    high_risk = [d for d in doc.get("ddl_changes", []) if d.get("risk") == "high"]
    if high_risk:
        reasons.append(
            "包含 %d 条高风险 DDL（%s），默认拒绝，需人工显式放行"
            % (len(high_risk), ", ".join(d.get("object", "?") for d in high_risk))
        )

    for tc in doc.get("table_changes", []):
        if tc.get("rows_truncated"):
            reasons.append("表 %s 的行级明细被截断，无法保证漏报为 0" % tc.get("table"))
        if not tc.get("primary_key"):
            reasons.append("表 %s 无主键，行定位不可靠（首版能力边界 5.3）" % tc.get("table"))

    return reasons


def consistency_warnings(doc: dict[str, Any]) -> list[str]:
    """summary 与明细的一致性自检，用于发现统计口径错误。"""
    warnings: list[str] = []
    totals = {"insert": 0, "update": 0, "delete": 0}
    for tc in doc.get("table_changes", []):
        counts = tc.get("counts", {})
        actual = {"insert": 0, "update": 0, "delete": 0}
        for row in tc.get("rows", []):
            actual[row["op"]] += 1
        if not tc.get("rows_truncated") and actual != {k: counts.get(k, 0) for k in actual}:
            warnings.append("表 %s 的 counts 与 rows 实际数量不一致：%s vs %s" % (tc.get("table"), counts, actual))
        for key in totals:
            totals[key] += counts.get(key, 0)

    summary = doc.get("summary", {})
    expected = {
        "rows_inserted": totals["insert"],
        "rows_updated": totals["update"],
        "rows_deleted": totals["delete"],
        "tables_affected": len(doc.get("table_changes", [])),
        "ddl_count": len(doc.get("ddl_changes", [])),
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            warnings.append("summary.%s=%s，与明细统计 %s 不一致" % (key, summary.get(key), value))

    for tc in doc.get("table_changes", []):
        for row in tc.get("rows", []):
            if row["op"] != "update":
                continue
            before, after = row.get("before") or {}, row.get("after") or {}
            actual_cols = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            if sorted(row.get("changed_columns", [])) != actual_cols:
                warnings.append(
                    "表 %s 行 %s 的 changed_columns=%s 与实际差异列 %s 不一致"
                    % (tc.get("table"), row.get("pk"), row.get("changed_columns"), actual_cols)
                )
    return warnings


def check_file(path: str, schema: dict[str, Any]) -> tuple[bool, bool]:
    """返回 (结构是否正确, 是否被门禁放行)。"""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    errors = structural_errors(doc, schema)
    name = os.path.basename(path)
    if errors:
        print(f"[FAIL] {name} 结构校验未通过：")
        for err in errors:
            print(f"       - {err}")
        return False, False

    warnings = consistency_warnings(doc)
    reasons = gate(doc)
    status = "ALLOW" if not reasons else "BLOCK"
    print(f"[ OK ] {name} 结构校验通过，门禁结论：{status}")
    for reason in reasons:
        print(f"       · 拒绝理由：{reason}")
    for warning in warnings:
        print(f"       ! 一致性告警：{warning}")
    return True, not reasons


def self_test() -> int:
    schema = load_schema()
    examples = os.path.join(HERE, "..", "examples")
    cases = {
        # 两个示例都包含 risk=high 的 ALTER TABLE，按 01 文档规则应被门禁拒绝（需人工放行）。
        "changeset.mysql.example.json": (True, False),
        "changeset.block-diff.example.json": (True, False),
        "changeset.conflict.example.json": (True, False),
    }
    failures = 0
    for name, (want_struct, want_allow) in cases.items():
        got = check_file(os.path.join(examples, name), schema)
        if got != (want_struct, want_allow):
            print(f"[SELFTEST FAIL] {name}: expected {(want_struct, want_allow)}, got {got}")
            failures += 1

    # 门禁必须拒绝高风险 DDL：mysql 示例含 ADD COLUMN（risk=high），故预期 BLOCK 才对。
    with open(os.path.join(examples, "changeset.mysql.example.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    if not gate(doc):
        print("[SELFTEST FAIL] 含高风险 DDL 的变更集本应被门禁拒绝")
        failures += 1

    doc_no_ddl = json.loads(json.dumps(doc))
    doc_no_ddl["ddl_changes"] = []
    doc_no_ddl["summary"]["ddl_count"] = 0
    if gate(doc_no_ddl):
        print("[SELFTEST FAIL] 去掉高风险 DDL 后本应放行")
        failures += 1

    print("self-test: " + ("PASS" if failures == 0 else f"FAIL ({failures})"))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="待校验的 ChangeSet JSON 文件")
    parser.add_argument("--self-test", action="store_true", help="用内置示例做回归自测")
    parser.add_argument("--allow-blocked", action="store_true", help="门禁拒绝时不影响退出码")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.files:
        parser.error("请提供待校验文件，或使用 --self-test")

    schema = load_schema()
    struct_ok = True
    all_allowed = True
    for path in args.files:
        ok, allowed = check_file(path, schema)
        struct_ok = struct_ok and ok
        all_allowed = all_allowed and allowed
    if not struct_ok:
        return 1
    if not all_allowed and not args.allow_blocked:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
