"""集中路径解析（v0.2.0）。

取代 v0.1.0 时代散落在 console.py / daemon/config.py / server/*.py 里的
``BUNDLE_ROOT`` 语义（DESIGN.md v0.2.0 packaging layout 一节有完整动机）。
两个锚点概念：

- **package 锚点**：随包分发的只读资源（web 门户、systemd 单元模板、示例
  配置）。wheel 安装后位于 site-packages/portable_console；源码树经
  console/console.py 兼容垫片运行得到完全相同的值 —— 无双行为。
- **用户锚点**：可写状态。``root_dir`` = 配置文件所在目录（控制卡脚本相对
  路径、server 子进程 cwd 的新锚点，对应旧 bundle_root）；``data_dir`` =
  运行时数据（health.json / console.token…），默认走 XDG，带 v0.1.0
  兼容规则（config 旁存在 ``data/`` 时优先使用）。

本模块零依赖、无副作用（只在函数被调用时读环境变量）。
"""
from __future__ import annotations

import os
from pathlib import Path

#: XDG 应用目录名（$XDG_DATA_HOME/portable-console、$XDG_CONFIG_HOME/portable-console）
APP_DIR_NAME = "portable-console"


def package_dir() -> Path:
    """包自身的目录（wheel: site-packages/portable_console；源码: console/src/portable_console）。"""
    return Path(__file__).resolve().parent


def web_root() -> Path:
    """门户静态资源根（index.html 所在，随包分发）。"""
    return package_dir() / "web"


def systemd_dir() -> Path:
    """systemd user-unit 模板目录（随包分发）。"""
    return package_dir() / "systemd"


def example_config() -> Path:
    """示例配置文件路径（随包分发；`install` 在目标缺失时拷贝它）。"""
    return package_dir() / "console.config.example.json"


def config_home() -> Path:
    """$XDG_CONFIG_HOME，缺省 ~/.config（XDG base dir 规范：空值视为未设置）。"""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    return Path(xdg) if xdg else Path.home() / ".config"


def default_config_path() -> Path:
    """`install` 未指定 --config 时的用户配置路径：
    $XDG_CONFIG_HOME/portable-console/console.config.json。"""
    return config_home() / APP_DIR_NAME / "console.config.json"


def user_unit_dir() -> Path:
    """systemd user-unit 安装目录：$XDG_CONFIG_HOME/systemd/user（缺省 ~/.config/...）。"""
    return config_home() / "systemd" / "user"


def root_dir(config_path: Path | None) -> Path:
    """相对路径解析锚点：配置文件所在目录；无配置文件时为当前工作目录。

    对应 v0.1.0 的 bundle_root（当时恒为 console/ 目录）。
    """
    if config_path is not None:
        return Path(config_path).parent
    return Path.cwd()


def default_data_dir(config_path: Path | None) -> Path:
    """未显式配置 ``data_dir`` 时的数据目录解析（优先级从高到低）：

    1. 给定配置文件且其旁边已存在 ``data/`` 目录 → ``<config_dir>/data``。
       **向后兼容规则**：v0.1.0 用户的数据一直在 bundle 相对 ``data/`` 里，
       升级后行为不变。
    2. 环境变量 XDG_DATA_HOME 非空 → ``$XDG_DATA_HOME/portable-console``。
    3. 否则 → ``~/.local/share/portable-console``（XDG 规范的默认值）。

    注意：config JSON 里显式的 ``data_dir`` 优先级更高（daemon/config.load
    直接采用并按 root_dir 解析相对值），本函数只负责"没写"的情况。
    """
    if config_path is not None:
        legacy = Path(config_path).parent / "data"
        if legacy.is_dir():
            return legacy
    xdg = os.environ.get("XDG_DATA_HOME", "")
    if xdg:
        return Path(xdg) / APP_DIR_NAME
    return Path.home() / ".local" / "share" / APP_DIR_NAME
