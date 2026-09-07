"""Portable Console — 可移植免 root 门户控制台（打包分发版）。

v0.2.0 起以 installable package 形式分发（src layout, 见 DESIGN.md
「v0.2.0 packaging layout」一节）：daemon/ server/ web/ systemd/ 与示例配置
全部进入本包，路径解析集中在 portable_console.paths。
零第三方运行时依赖（DESIGN.md §0 硬约束, stdlib only）。
"""

__version__ = "0.2.0"
