#!/usr/bin/env python3
"""一键运行本目录下全部可执行件的自测。

⚠️ **这些是契约自洽性检查，不构成对系统行为的任何验证。**
它们校验的是「本仓库的文件是否符合本仓库自己写下的规则」，
不代表任何实现是正确的，也不代表差异真的没有漏报。
真正的正确性证据只能来自 02 文档的 spike 与真实环境实测。
「全部通过」这句话的保证范围仅限于此。

无需数据库环境，只依赖 Python 3.9+（jsonschema 可选，缺失时降级为最小结构检查）。

用法：
    python3 docs/agent-sandbox/tools/check_all.py
退出码：0 全部通过；1 存在失败。
"""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

CHECKS = [
    ("ChangeSet 契约自洽性检查（含示例回归）", [sys.executable, os.path.join(BASE, "tools", "validate_changeset.py"), "--self-test"]),
    ("A/B 路径比对器（含单侧缺失/未折叠负样本）", [sys.executable, os.path.join(BASE, "spike", "compare_changesets.py"), "--self-test"]),
    ("沙盒状态机不变式（图结构 + 标签约定）", [sys.executable, os.path.join(BASE, "tools", "validate_state_machine.py")]),
    ("POC 验收判定器（含失配率上界推导）", [sys.executable, os.path.join(BASE, "tools", "poc_gate.py"), "--self-test"]),
    ("工作量蒙特卡洛模拟", [sys.executable, os.path.join(BASE, "tools", "estimate_montecarlo.py"), "--self-test"]),
]


def main() -> int:
    failures = []
    for name, cmd in CHECKS:
        print(f"\n=== {name} ===")
        result = subprocess.run(cmd, cwd=BASE)
        if result.returncode != 0:
            failures.append(name)

    print("\n" + "=" * 40)
    if failures:
        print(f"FAIL：{len(failures)}/{len(CHECKS)} 项未通过 -> {', '.join(failures)}")
        return 1
    print(f"PASS：{len(CHECKS)}/{len(CHECKS)} 项契约自洽性检查通过")
    print("提醒：这不构成对系统行为的验证，只说明文件与本仓库自定的规则一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
