"""NVIDIA GPU collector (port of parse-stats.py get_gpu_stats + _get_gpu_card_names).

- GPU count is enumerated dynamically from nvidia-smi output at runtime —
  never hardcoded, never truncated to a fixed tile count (laptops with 0/1/2
  GPUs all work).
- Append-only CSV column discipline preserved from source: the query
  requests 12 columns ending in ``temperature.memory`` (parts[11]); older
  drivers that reject it would break the whole row parse, so the source's
  single-query layout is kept verbatim.
- Card marketing names resolve via lspci (bus-id join) → nvidia-smi name →
  ``gpu<index>`` fallback chain (source:784-838).
- probe: nvidia-smi present + one successful query; laptops without NVIDIA
  simply get no ``gpus`` key in health.json (DESIGN §0 honest degradation).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

from portable_console.daemon.plugins.base import run_argv

if False:  # pragma: no cover - type-only
    from portable_console.daemon.config import ConsoleConfig

_GPU_QUERY = [
    "nvidia-smi",
    "--query-gpu=index,utilization.gpu,memory.total,memory.used,memory.free,"
    "temperature.gpu,power.draw,pcie.link.gen.current,pcie.link.gen.max,"
    "pcie.link.width.current,pcie.link.width.max,temperature.memory",
    "--format=csv,noheader,nounits",
]
_LSPCI_TIMEOUT_S = 3.0
_SMI_TIMEOUT_S = 5.0

_gpu_card_names_cache: dict[int, str] | None = None  # hardware is static


def _fval(s: str) -> float:
    """Numeric CSV field; tolerates '[N/A]' / '[Not Supported]' → 0.0."""
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _ival(s: str) -> int | None:
    """Integer CSV field; None when missing/unparsable (PCIe link state)."""
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _card_names() -> dict[int, str] | None:
    """nvidia-smi index -> marketing name from lspci, cached (source:784-838).

    lspci reports e.g. 'GA100 [CMP 170HX]' while nvidia-smi only says
    'NVIDIA Graphics Device'; join them on the PCI bus id. Falls back to the
    nvidia-smi name, then 'gpu<index>' (applied by the caller).
    """
    global _gpu_card_names_cache
    if _gpu_card_names_cache is not None:
        return _gpu_card_names_cache

    by_bus: dict[str, str] = {}
    try:
        r = run_argv(["lspci"], timeout=_LSPCI_TIMEOUT_S)
        for line in r.stdout.splitlines():
            if "nvidia" not in line.lower():
                continue
            m = re.match(r"^([0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F])\s", line)
            if not m:
                continue
            name = None
            m_name = re.search(r"\[([^\]]+)\]", line)  # marketing bracket name
            if m_name:
                name = m_name.group(1)
            else:
                m_name = re.search(r"NVIDIA Corporation\s+(.+)", line)
                if m_name:
                    name = m_name.group(1).split("(")[0].strip()
            if name:
                by_bus[m.group(1)] = name
    except (OSError, subprocess.TimeoutExpired):
        pass

    names: dict[int, str] = {}
    try:
        r = run_argv(
            ["nvidia-smi", "--query-gpu=index,pci.bus_id,name",
             "--format=csv,noheader,nounits"],
            timeout=_SMI_TIMEOUT_S,
        )
        for line in r.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                idx = int(float(parts[0]))
            except ValueError:
                continue
            real = next(
                (n for b, n in by_bus.items() if parts[1].endswith(b)), parts[2]
            )
            names[idx] = real
    except (OSError, subprocess.TimeoutExpired):
        pass

    _gpu_card_names_cache = names or None
    return _gpu_card_names_cache


def get_gpu_stats() -> list[dict[str, Any]]:
    """GPU metric rows in the golden health.json schema (source:841-891).

    Any nvidia-smi failure yields [] — never raises.
    """
    try:
        r = run_argv(_GPU_QUERY, timeout=_SMI_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return []

    gpus: list[dict[str, Any]] = []
    for line in r.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 12:
            continue
        vram_total_mib = _fval(parts[2])
        vram_used_mib = _fval(parts[3])
        temp_gpu_c = _fval(parts[5])
        gpu_idx = int(_fval(parts[0]))
        gpus.append({
            "card": (_card_names() or {}).get(gpu_idx, f"gpu{gpu_idx}"),
            "use_pct": _fval(parts[1]),
            "vram_alloc_pct": (
                round((vram_used_mib / vram_total_mib) * 100, 1)
                if vram_total_mib else 0
            ),
            # CMP 170HX: junction/core share the case sensor; memory temp is
            # the distinct HBM sensor (parts[11] is the append-only last col).
            "temp_edge_c": temp_gpu_c,
            "temp_junction_c": temp_gpu_c,
            "temp_memory_c": _fval(parts[11]),
            "power_w": _fval(parts[6]),
            "vram_total_gb": round(vram_total_mib * 1048576 / 1e9, 1),
            "vram_used_gb": round(vram_used_mib * 1048576 / 1e9, 1),
            "pcie_gen_cur": _ival(parts[7]),
            "pcie_gen_max": _ival(parts[8]),
            "pcie_width_cur": _ival(parts[9]),
            "pcie_width_max": _ival(parts[10]),
        })
    return gpus


class NvidiaCollector:
    """Enabled iff nvidia-smi exists and answers one successful query."""

    name = "nvidia"

    def probe(self, cfg: "ConsoleConfig") -> bool:
        del cfg
        if shutil.which("nvidia-smi") is None:
            return False
        try:
            r = run_argv(_GPU_QUERY, timeout=_SMI_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0 and bool(r.stdout.strip())

    def collect(self, cfg: "ConsoleConfig", ctx: dict[str, Any]) -> dict[str, Any]:
        del cfg, ctx
        gpus = get_gpu_stats()
        return {"gpus": gpus} if gpus else {}


def reset_caches() -> None:
    """Drop the static-hardware name cache (test hook)."""
    global _gpu_card_names_cache
    _gpu_card_names_cache = None

