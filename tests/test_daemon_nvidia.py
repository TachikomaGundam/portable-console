"""NVIDIA collector tests — nvidia-smi/lspci fully faked via monkeypatch.

Key coverage required by the brief: dynamic index enumeration (2 GPUs ->
2 tiles, 4 -> 4; no hardcoded count / no [:4] truncation), golden-schema
element keys, append-only column discipline, lspci name fallback chain,
probe-off behaviour on machines without NVIDIA.
"""
from __future__ import annotations

import subprocess

import pytest

from daemon.config import ConsoleConfig
from daemon.plugins import nvidia
from daemon.plugins.nvidia import NvidiaCollector, get_gpu_stats

GOLDEN_GPU_KEYS = {
    "card", "use_pct", "vram_alloc_pct", "temp_edge_c", "temp_junction_c",
    "temp_memory_c", "power_w", "vram_total_gb", "vram_used_gb",
    "pcie_gen_cur", "pcie_gen_max", "pcie_width_cur", "pcie_width_max",
}


def _gpu_row(idx: int, util: int = 50, mem_total: int = 65536,
             mem_used: int = 32768, temp: int = 60, power: float = 120.5,
             mem_temp: int = 68) -> str:
    return (f"{idx}, {util}, {mem_total}, {mem_used}, "
            f"{mem_total - mem_used}, {temp}, {power}, 4, 4, 16, 16, "
            f"{mem_temp}")


def _smi_output(n: int) -> str:
    return "".join(f"{_gpu_row(i)}\n" for i in range(n))


def _fake_smi(n: int, lspci_lines: str = ""):
    def fake(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[0] == "lspci":
            return subprocess.CompletedProcess(argv, 0, lspci_lines, "")
        if argv[0] == "nvidia-smi" and "--query-gpu=index,pci.bus_id,name" in argv:
            names = "".join(
                f"{i}, 00000000:{i:02d}:00.0, NVIDIA Graphics Device\n"
                for i in range(n))
            return subprocess.CompletedProcess(argv, 0, names, "")
        return subprocess.CompletedProcess(argv, 0, _smi_output(n), "")
    return fake


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    nvidia.reset_caches()


def test_gpu_count_is_dynamic_two_and_four(make_config: ConsoleConfig,
                                           monkeypatch: pytest.MonkeyPatch
                                           ) -> None:
    for n in (2, 4):
        nvidia.reset_caches()
        monkeypatch.setattr(nvidia, "run_argv", _fake_smi(n))
        gpus = get_gpu_stats()
        assert len(gpus) == n
        # indices 0..n-1 all present, names via the smi fallback (no lspci)
        assert [g["card"] for g in gpus] == ["NVIDIA Graphics Device"] * n


def test_element_keys_match_golden_and_values_parsed(
        make_config: ConsoleConfig,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nvidia, "run_argv", _fake_smi(1))
    gpu = get_gpu_stats()[0]
    assert set(gpu) == GOLDEN_GPU_KEYS
    assert gpu["use_pct"] == 50.0
    assert gpu["temp_memory_c"] == 68.0        # parts[11] append-only column
    assert gpu["power_w"] == 120.5
    assert gpu["vram_total_gb"] == 68.7        # 65536 MiB -> 68.7 GB
    assert gpu["vram_used_gb"] == 34.4
    assert gpu["vram_alloc_pct"] == 50.0
    assert gpu["pcie_gen_cur"] == 4 and gpu["pcie_width_max"] == 16
    assert gpu["temp_edge_c"] == gpu["temp_junction_c"] == 60.0


def test_query_last_column_is_temperature_memory() -> None:
    assert nvidia._GPU_QUERY[1].split(",").pop() == "temperature.memory"
    assert len(nvidia._GPU_QUERY[1].split(",")) == 12


def test_lspci_marketing_name_join_and_fallback_chain(
        monkeypatch: pytest.MonkeyPatch) -> None:
    lspci = ("00:02.0 USB controller: Intel\n"
             "00:00.0 3D controller: NVIDIA Corporation GA100 [CMP 170HX] (rev a1)\n")
    monkeypatch.setattr(nvidia, "run_argv", _fake_smi(1, lspci))
    assert get_gpu_stats()[0]["card"] == "CMP 170HX"

    nvidia.reset_caches()                        # lspci missing -> smi name
    monkeypatch.setattr(nvidia, "run_argv", _fake_smi(1, ""))
    assert get_gpu_stats()[0]["card"] == "NVIDIA Graphics Device"


def test_probe_requires_binary_and_successful_query(
        make_config: ConsoleConfig,
        monkeypatch: pytest.MonkeyPatch) -> None:
    c = NvidiaCollector()
    monkeypatch.setattr(nvidia.shutil, "which", lambda x: None)
    assert c.probe(make_config) is False

    monkeypatch.setattr(nvidia.shutil, "which", lambda x: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(nvidia, "run_argv", _fake_smi(2))
    assert c.probe(make_config) is True

    def boom(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise OSError("no driver")
    monkeypatch.setattr(nvidia, "run_argv", boom)
    assert c.probe(make_config) is False


def test_collect_empty_output_omits_section(
        make_config: ConsoleConfig,
        monkeypatch: pytest.MonkeyPatch) -> None:
    def empty(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(nvidia, "run_argv", empty)
    assert NvidiaCollector().collect(make_config, {}) == {}


def test_malformed_rows_skipped_not_crash(
        monkeypatch: pytest.MonkeyPatch) -> None:
    out = "garbage line\n" + _gpu_row(1) + "\n3, 10, 8000, 100, 7900, 55\n"  # short
    def fake(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if argv[0] == "lspci":
            raise OSError("absent")
        if "--query-gpu=index,pci.bus_id,name" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")  # names unknown
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(nvidia, "run_argv", fake)
    gpus = get_gpu_stats()
    assert len(gpus) == 1 and gpus[0]["card"] == "gpu1"
