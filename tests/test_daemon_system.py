"""System collector tests: /proc parsing + statvfs disk auto-discovery.

The whole plugin is driven against a fabricated /proc tree in tmp_path —
no dependency on the host's real /proc beyond its mere existence.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from portable_console.daemon.config import ConsoleConfig
from portable_console.daemon.plugins import system
from portable_console.daemon.plugins.system import SystemCollector

MOUNTS = "\n".join([
    "/dev/sda2 / ext4 rw,relatime 0 0",
    "/dev/sdb1 /mnt/data ext4 rw,relatime 0 0",
    "/dev/sda2 /home/bind ext4 rw 0 0",           # dup device -> skipped
    "tmpfs /run tmpfs rw 0 0",                    # pseudo -> skipped
    "proc /proc proc rw 0 0",                     # pseudo -> skipped
    "overlay /var/lib/docker/overlay2/x overlay rw 0 0",
    "udev /dev devtmpfs rw 0 0",                  # pseudo -> skipped
    "/dev/sdc1 /mnt/weird\\040name xfs rw 0 0",   # octal escape, kept
])

MEMINFO = """MemTotal:       1000000 kB
MemFree:         200000 kB
MemAvailable:    400000 kB
SwapTotal:       100000 kB
SwapFree:         50000 kB
HugePages_Total:       0
"""


@pytest.fixture
def fake_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "loadavg").write_text("0.50 0.40 0.30 1/100 12345\n")
    (proc / "meminfo").write_text(MEMINFO)
    (proc / "uptime").write_text("12345.67 98765.43\n")
    (proc / "mounts").write_text(MOUNTS + "\n")
    monkeypatch.setattr(system, "PROC_ROOT", str(proc))
    sizes = {
        "/": (1_000_000, 500_000),
        "/mnt/data": (2_000_000, 1_000_000),
        "/mnt/weird name": (4_000_000, 3_900_000),
    }

    def fake_disk_usage(path: str) -> SimpleNamespace:
        total, used = sizes[path]
        return SimpleNamespace(total=total, used=used, free=total - used)

    monkeypatch.setattr(system.shutil, "disk_usage", fake_disk_usage)
    return proc


def test_probe_always_true(make_config: ConsoleConfig) -> None:
    assert SystemCollector().probe(make_config) is True


def test_collect_cpu_load_mem_swap_uptime(fake_proc: Path,
                                          make_config: ConsoleConfig) -> None:
    health = SystemCollector().collect(make_config, {})
    assert health["load_1m"] == 0.5
    assert health["load_5m"] == 0.4
    assert health["load_15m"] == 0.3
    assert health["mem_total"] == 1000000 * 1024          # kB -> bytes
    assert health["mem_available"] == 400000 * 1024
    assert health["mem_used"] == 600000 * 1024
    assert health["mem_used_pct"] == 60.0
    assert health["swap_total"] == 100000 * 1024
    assert health["swap_used_pct"] == 50.0
    assert health["uptime_s"] == 12345.67
    assert health["cpu_cores"] >= 1


def test_disks_autodiscovered_pseudo_excluded(
        fake_proc: Path, make_config: ConsoleConfig) -> None:
    health = SystemCollector().collect(make_config, {})
    paths = [d["path"] for d in health["disks"]]
    assert paths == ["/", "/mnt/data", "/mnt/weird name"]  # OS first
    assert health["disks"][0]["label"] == "OS /"
    assert health["disks"][1]["label"] == "Data /mnt/data"
    assert health["disks"][0] == {
        "path": "/", "label": "OS /", "total": 1_000_000, "used": 500_000,
        "free": 500_000, "used_pct": 50.0,
    }
    # backward-compat single-disk fields point at the OS disk
    assert health["disk_total"] == 1_000_000
    assert health["disk_used"] == 500_000
    assert health["disk_used_pct"] == 50.0


def test_missing_proc_files_degrade_quietly(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch,
                                            make_config: ConsoleConfig) -> None:
    empty = tmp_path / "proc"
    empty.mkdir()
    monkeypatch.setattr(system, "PROC_ROOT", str(empty))
    monkeypatch.setattr(system, "_real_mounts", lambda: [])
    health = SystemCollector().collect(make_config, {})
    assert health["cpu_cores"] >= 1
    assert health["disks"] == []
    assert "load_1m" not in health          # honest absence, no fake 0
    assert "disk_total" not in health
