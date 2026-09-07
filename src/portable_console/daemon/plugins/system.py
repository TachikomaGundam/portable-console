"""System collector: CPU / load / memory / swap / disks / uptime.

Port of parse-stats.py `get_system_health()` (lines 1382-1446) — the /proc +
disk portion only; GPUs, fans, power and the LLM summary live in their own
plugins.

Deviations from source (DESIGN.md §0 portability wins):
- disks are AUTO-DISCOVERED from /proc/mounts via os.statvfs on real
  filesystems (pseudo filesystems and container/volume-manager mounts
  excluded, deduped by source device) instead of the source's hardcoded
  ``[("/", "OS /"), ("/data", "Data /data")]`` list.
- the first disk (path "/") keeps label "OS /"; any other real mount is
  labelled "Data <path>", matching the golden health.json naming scheme.
"""
from __future__ import annotations

import os
import shutil
from typing import Any

if False:  # pragma: no cover - type-only
    from portable_console.daemon.config import ConsoleConfig

# Module-level so tests can point it at a fabricated tree.
PROC_ROOT = "/proc"

# Filesystem types that never carry user data (DESIGN: auto-discover *real*
# filesystems; these are kernel/API pseudo-mounts).
_PSEUDO_FSTYPES = frozenset({
    "autofs", "binfmt_misc", "bpf", "cgroup", "cgroup2", "configfs",
    "debugfs", "devpts", "devtmpfs", "efivarfs", "fat", "fat32",
    "fuse.gvfsd-fuse",
    "fuse.portal", "hugetlbfs", "mqueue", "msdos", "nsfs", "overlay", "proc",
    "pstore", "ramfs", "rpc_pipefs", "securityfs", "selinuxfs", "squashfs",
    "sysfs", "tmpfs", "tracefs", "vfat",  # FAT family: EFI/recovery, not data
})

# Mount trees that are infrastructure, not data volumes.
_PSEUDO_PATHS = ("/proc", "/sys", "/dev", "/run", "/snap",
                 "/var/lib/docker", "/var/lib/containerd")

DISK_BYTES_PER_KB = 1024


def _read_lines(relpath: str) -> list[str]:
    path = os.path.join(PROC_ROOT, relpath)
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()


def _unescape_mount(path: str) -> str:
    """/proc/mounts escapes spaces and friends as octal (\\040)."""
    return path.encode("ascii", "backslashreplace").decode("unicode_escape")


def _real_mounts() -> list[tuple[str, str]]:
    """(device, mountpoint) of plausible data filesystems, '/' first."""
    out: list[tuple[str, str]] = []
    seen_dev: set[str] = set()
    try:
        lines = _read_lines("mounts")
    except OSError:
        return out
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        dev, mnt, fstype = fields[0], _unescape_mount(fields[1]), fields[2]
        if fstype in _PSEUDO_FSTYPES:
            continue
        if mnt != "/" and any(
            mnt == pre or mnt.startswith(pre + "/") for pre in _PSEUDO_PATHS
        ):
            continue
        if dev in seen_dev:  # bind mount / repeated device -> keep first
            continue
        seen_dev.add(dev)
        out.append((dev, mnt))
    out.sort(key=lambda dm: (dm[1] != "/", dm[1]))
    return out


def collect_disks() -> list[dict[str, Any]]:
    """statvfs every real mount into the golden disk schema."""
    disks: list[dict[str, Any]] = []
    for _, mnt in _real_mounts():
        try:
            usage = shutil.disk_usage(mnt)
        except OSError:
            continue
        disks.append({
            "path": mnt,
            "label": "OS /" if mnt == "/" else f"Data {mnt}",
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "used_pct": round(100 * usage.used / usage.total, 1) if usage.total else 0,
        })
    return disks


def _meminfo() -> dict[str, int]:
    info: dict[str, int] = {}
    for line in _read_lines("meminfo"):
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        value = rest.strip().replace(" kB", "").strip()
        try:
            info[key.strip()] = int(value) * DISK_BYTES_PER_KB  # kB -> bytes
        except ValueError:
            pass
    return info


class SystemCollector:
    """Always-enabled collector (DESIGN §3: system section is unconditional)."""

    name = "system"

    def probe(self, cfg: "ConsoleConfig") -> bool:
        del cfg
        return True

    def collect(self, cfg: "ConsoleConfig", ctx: dict[str, Any]) -> dict[str, Any]:
        del cfg, ctx
        health: dict[str, Any] = {}
        health["cpu_cores"] = os.cpu_count() or 1

        # Load average from /proc/loadavg
        try:
            parts = _read_lines("loadavg")[0].split()
            health["load_1m"] = float(parts[0])
            health["load_5m"] = float(parts[1])
            health["load_15m"] = float(parts[2])
        except (OSError, IndexError, ValueError):
            pass

        # Memory + swap from /proc/meminfo (kB -> bytes)
        try:
            meminfo = _meminfo()
            mem_total = meminfo.get("MemTotal", 0)
            mem_available = meminfo.get("MemAvailable") or meminfo.get("MemFree", 0)
            health["mem_total"] = mem_total
            health["mem_available"] = mem_available
            health["mem_used"] = mem_total - mem_available
            health["mem_used_pct"] = (
                round(100 * health["mem_used"] / mem_total, 1) if mem_total else 0
            )
            swap_total = meminfo.get("SwapTotal", 0)
            swap_free = meminfo.get("SwapFree", 0)
            health["swap_total"] = swap_total
            health["swap_free"] = swap_free
            health["swap_used_pct"] = (
                round(100 * (1 - swap_free / swap_total), 1) if swap_total else 0
            )
        except OSError:
            pass

        # Disks — auto-discovered; single-disk compat fields point at OS disk
        disks = collect_disks()
        health["disks"] = disks
        if disks:
            health["disk_total"] = disks[0]["total"]
            health["disk_used"] = disks[0]["used"]
            health["disk_used_pct"] = disks[0]["used_pct"]

        # Uptime
        try:
            health["uptime_s"] = float(
                _read_lines("uptime")[0].split()[0]
            )
        except (OSError, IndexError, ValueError):
            pass

        return health


def collect(cfg: "ConsoleConfig", ctx: dict[str, Any]) -> dict[str, Any]:
    """Module-level alias kept for harness wiring symmetry."""
    return SystemCollector().collect(cfg, ctx)
