#!/usr/bin/env python3
"""v0.1.0 兼容垫片：`python3 console.py daemon once` 等在源码树下继续可用。

v0.2.0 起全部实现位于 src/portable_console/（DESIGN.md「v0.2.0 packaging
layout」）；本文件只把 src/ 挂上 sys.path 并原样委托给 portable_console.cli。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from portable_console.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
