#!/usr/bin/env python3
"""一键运行本目录下全部可执行件的自测。

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
    ("ChangeSet 门禁校验（含示例回归）", [sys.executable, os.path.join(BASE, "tools", "validate_changeset.py"), "--self-test"]),
    ("A/B 路径比对器（含注入漏报负样本）", [sys.executable, os.path.join(BASE, "spike", "compare_changesets.py"), "--self-test"]),
    ("沙盒状态机不变式", [sys.executable, os.path.join(BASE, "tools", "validate_state_machine.py")]),
    ("POC 验收判定器", [sys.executable, os.path.join(BASE, "tools", "poc_gate.py"), "--self-test"]),
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
    print(f"PASS：{len(CHECKS)}/{len(CHECKS)} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
