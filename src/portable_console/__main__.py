"""``python3 -m portable_console`` 入口（源码树运行与 ExecStart 回退共用）。"""
import sys

from portable_console.cli import main

if __name__ == "__main__":
    sys.exit(main())
