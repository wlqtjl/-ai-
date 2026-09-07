#!/usr/bin/env python3
"""ChangeSet 契约自洽性检查器。

⚠️ 本脚本做的是**契约自洽性检查**，不构成对系统行为的任何验证。
它校验的是「JSON 文件是否符合本仓库自己定义的规则」，而不是「实现是否正确」或「差异是否真的没有漏报」。
真正的正确性证据只能来自 02 文档的 fixture 全集比对与真实环境实测。

三层检查：
1. 结构检查：对 schema/changeset.schema.json 做 JSON Schema 验证（缺少 jsonschema 依赖时降级为内建的最小结构检查）。
2. 净状态差不变式：同一表内主键唯一、before != after —— 对应 01 文档第 3 节的规范形式。
3. 门禁规则（gate）：实现 01-changeset-model.md 中的硬规则，区分「阻断」与「需人工逐条确认」。

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


def net_diff_violations(doc: dict[str, Any]) -> list[str]:
    """净状态差不变式检查（01 文档第 3 节）。

    ChangeSet 的规范形式是净状态差而非操作序列，因此：
    - 同一张表内每个主键最多出现一次；
    - update 的 before 与 after 不得完全相同（净效果为零的行必须在折叠时剔除）。
    """
    violations: list[str] = []
    for tc in doc.get("table_changes", []):
        seen: dict[str, int] = {}
        for row in tc.get("rows", []):
            key = json.dumps(row.get("pk"), sort_keys=True, ensure_ascii=False)
            seen[key] = seen.get(key, 0) + 1
        for key, count in seen.items():
            if count > 1:
                violations.append(
                    "表 %s 的主键 %s 出现 %d 次，违反净状态差规范（逻辑日志路径未做折叠？）"
                    % (tc.get("table"), key, count)
                )
        for row in tc.get("rows", []):
            if row.get("op") == "update" and (row.get("before") or {}) == (row.get("after") or {}):
                violations.append("表 %s 行 %s 的 before 与 after 相同，净效果为零，应在折叠时剔除" % (tc.get("table"), row.get("pk")))
    return violations


def ddl_acks_required(doc: dict[str, Any]) -> list[str]:
    """高风险 DDL 需要审批人逐条确认 —— 注意这是『需额外确认』而非『阻断整个变更集』。

    第一轮把两者混为一谈，与『整变更集全量处理』叠加后导致：
    Agent 只要碰过一次 DDL，整个变更集就被拦死，而 schema 变更恰是核心价值场景。
    """
    return [
        "高风险 DDL 需审批人逐条确认：%s（%s）" % (d.get("object", "?"), d.get("ddl_type"))
        for d in doc.get("ddl_changes", [])
        if d.get("risk") == "high"
    ]


def gate(doc: dict[str, Any]) -> list[str]:
    """返回**阻断**理由列表；空列表表示允许进入审批流程（可能仍需 DDL 逐条确认）。"""
    reasons: list[str] = []
    integrity = doc.get("integrity", {})

    if integrity.get("completeness") != "complete":
        reasons.append(
            "integrity.completeness=%s，差异可能漏报，禁止进入审批与合并流程"
            % integrity.get("completeness")
        )

    if doc.get("conflicts"):
        reasons.append("存在 %d 条冲突，首版策略为冲突即拒绝，需基于最新时间点重建沙盒" % len(doc["conflicts"]))

    reasons.extend(net_diff_violations(doc))

    for tc in doc.get("table_changes", []):
        if tc.get("rows_truncated"):
            reasons.append("表 %s 的行级明细被截断，无法保证漏报为 0" % tc.get("table"))
        if not tc.get("primary_key"):
            reasons.append("表 %s 无主键，行定位不可靠（首版能力边界 6.4）" % tc.get("table"))

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
    acks = ddl_acks_required(doc)
    status = "ALLOW" if not reasons else "BLOCK"
    print(f"[ OK ] {name} 结构检查通过，门禁结论：{status}")
    for reason in reasons:
        print(f"       · 阻断理由：{reason}")
    for ack in acks:
        print(f"       ? 需人工确认：{ack}")
    for warning in warnings:
        print(f"       ! 一致性告警：{warning}")
    return True, not reasons


def self_test() -> int:
    schema = load_schema()
    examples = os.path.join(HERE, "..", "examples")
    cases = {
        # 第二轮修订：高风险 DDL 不再阻断整个变更集，只要求逐条确认，故前两个示例应为 ALLOW。
        "changeset.mysql.example.json": (True, True),
        "changeset.block-diff.example.json": (True, True),
        # 该示例同时含行级冲突与截断，两条都是真正的阻断项。
        "changeset.conflict.example.json": (True, False),
    }
    failures = 0
    for name, (want_struct, want_allow) in cases.items():
        got = check_file(os.path.join(examples, name), schema)
        if got != (want_struct, want_allow):
            print(f"[SELFTEST FAIL] {name}: expected {(want_struct, want_allow)}, got {got}")
            failures += 1

    with open(os.path.join(examples, "changeset.mysql.example.json"), encoding="utf-8") as fh:
        doc = json.load(fh)

    # 高风险 DDL 应产生「需确认」而非「阻断」。
    if not ddl_acks_required(doc):
        print("[SELFTEST FAIL] 含高风险 DDL 的变更集本应要求逐条确认")
        failures += 1
    if gate(doc):
        print("[SELFTEST FAIL] 仅含高风险 DDL 不应阻断整个变更集（缺陷 12：组合路径死锁）")
        failures += 1

    # 净状态差不变式：同一主键出现两次必须被拦下。
    dup = json.loads(json.dumps(doc))
    first_table = dup["table_changes"][0]
    first_table["rows"].append(json.loads(json.dumps(first_table["rows"][0])))
    if not net_diff_violations(dup):
        print("[SELFTEST FAIL] 同一主键重复出现本应违反净状态差不变式")
        failures += 1

    # before == after 的 update 必须被拦下。
    noop = json.loads(json.dumps(doc))
    for tc in noop["table_changes"]:
        for row in tc["rows"]:
            if row["op"] == "update":
                row["after"] = json.loads(json.dumps(row["before"]))
                break
    if not net_diff_violations(noop):
        print("[SELFTEST FAIL] before==after 的 update 本应违反净状态差不变式")
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
