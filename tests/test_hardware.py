# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
import json
import os
import sys
import threading
import time

import pytest

from pulsar import hardware as H

OS_RELEASE = '''PRETTY_NAME="Ubuntu 24.04.1 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
VERSION="24.04.1 LTS (Noble Numbat)"
# a comment
ID=ubuntu
ID_LIKE=debian
HOME_URL='https://www.ubuntu.com/'
'''

MEMINFO = '''MemTotal:       16480972 kB
MemFree:         1048576 kB
MemAvailable:   15410152 kB
Buffers:          123456 kB
Cached:          2345678 kB
'''

CPUINFO_X86 = '''processor	: 0
vendor_id	: AuthenticAMD
model name	: AMD Ryzen 9 7950X 16-Core Processor
physical id	: 0
core id		: 0
cpu cores	: 16

processor	: 1
vendor_id	: AuthenticAMD
model name	: AMD Ryzen 9 7950X 16-Core Processor
physical id	: 0
core id		: 0
cpu cores	: 16

processor	: 2
model name	: AMD Ryzen 9 7950X 16-Core Processor
physical id	: 0
core id		: 1
cpu cores	: 16
'''

CPUINFO_PI = '''processor	: 0
BogoMIPS	: 108.00
CPU implementer	: 0x41
CPU architecture: 8
CPU part	: 0xd0b

processor	: 1
CPU implementer	: 0x41
CPU part	: 0xd0b

Hardware	: BCM2835
Model		: Raspberry Pi 5 Model B Rev 1.0
'''

CPUINFO_GRAVITON = '''processor	: 0
CPU implementer	: 0x41
CPU part	: 0xd40

processor	: 1
CPU implementer	: 0x41
CPU part	: 0xd40
'''

SYSCTL_MAC = '''machdep.cpu.brand_string: Apple M3 Max
hw.memsize: 68719476736
hw.ncpu: 16
hw.physicalcpu: 16
hw.perflevel0.physicalcpu: 12
hw.perflevel1.physicalcpu: 4
hw.optional.arm64: 1
'''

VM_STAT = '''Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               100000.
Pages active:                             900000.
Pages inactive:                           800000.
Pages speculative:                         50000.
Pages throttled:                               0.
'''

NVIDIA_SMI = '''NVIDIA GeForce RTX 4090, 24564, 550.54.14
NVIDIA H100 80GB HBM3, 81559, 550.54.15
NVIDIA GB10, [N/A], 580.95.05
'''

ROCM_SMI = json.dumps({
    "card0": {"Card Series": "Radeon RX 7900 XTX", "Card Model": "0x744c",
              "Card Vendor": "Advanced Micro Devices, Inc. [AMD/ATI]",
              "VRAM Total Memory (B)": "25753026560", "VRAM Total Used Memory (B)": "1000"},
    "card1": {"Card series": "N/A", "Marketing Name": "AMD Instinct MI300X", "VRAM Total Memory (B)": "206141652992"},
    "system": {"Driver version": "6.7.0"},
})

LSPCI_MM = '''00:02.0 "VGA compatible controller" "Intel Corporation" "Alder Lake-P GT2 [Iris Xe Graphics]" -r0c "Lenovo" "Device 3b15"
01:00.0 "VGA compatible controller" "NVIDIA Corporation" "AD102 [GeForce RTX 4090]" -ra1 "Micro-Star International Co., Ltd. [MSI]" "Device 5103"
03:00.0 "VGA compatible controller" "Advanced Micro Devices, Inc. [AMD/ATI]" "Navi 31 [Radeon RX 7900 XT/7900 XTX/7900M]" -rc8 "Sapphire" "Device e471"
04:00.0 "VGA compatible controller" "ASPEED Technology, Inc." "ASPEED Graphics Family" -r52 "ASPEED" "ASPEED Graphics Family"
9c4c:00:00.0 "3D controller" "Microsoft Corporation" "Basic Render Driver" "" ""
00:1f.3 "Audio device" "Intel Corporation" "Alder Lake PCH-P High Definition Audio Controller" -r01 "" ""
broken "line
'''

SP_APPLE = json.dumps({"SPDisplaysDataType": [{
    "_name": "Apple M3 Max", "sppci_model": "Apple M3 Max", "sppci_cores": "40",
    "spdisplays_vendor": "sppci_vendor_Apple", "spdisplays_mtlgpufamilysupport": "spdisplays_metal3"}]})

SP_INTEL_MAC = json.dumps({"SPDisplaysDataType": [
    {"sppci_model": "Intel UHD Graphics 630", "spdisplays_vendor": "Intel", "spdisplays_vram": "1536 MB"},
    {"sppci_model": "AMD Radeon Pro 5500M", "spdisplays_vendor": "sppci_vendor_amd", "spdisplays_vram": "8 GB",
     "spdisplays_mtlgpufamilysupport": "spdisplays_metal3"}]})

NV_PROC = '''Model: 		 NVIDIA GeForce RTX 3090
IRQ:   		 139
GPU UUID: 	 GPU-00000000-1111-2222-3333-444444444444
Video BIOS: 	 94.02.42.00.a9
'''

NV_VERSION = "NVRM version: NVIDIA UNIX x86_64 Kernel Module  550.54.14  Thu Feb 22 01:44:30 UTC 2024\n"


def test_parse_os_release_and_meminfo():
    rel = H.parse_os_release(OS_RELEASE)
    assert rel["PRETTY_NAME"] == "Ubuntu 24.04.1 LTS" and rel["ID"] == "ubuntu" and rel["VERSION_ID"] == "24.04"
    assert rel["HOME_URL"] == "https://www.ubuntu.com/"
    assert H.parse_meminfo(MEMINFO) == (15.7, 14.7)
    total, avail = H.parse_meminfo("MemTotal: 8388608 kB\nMemFree: 1048576 kB\nCached: 1048576 kB\n")
    assert total == 8.0 and avail == 2.0            # old kernels without MemAvailable
    assert H.parse_meminfo("") == (0.0, 0.0)


def test_parse_cpuinfo_x86_arm_and_boards():
    x86 = H.parse_cpuinfo(CPUINFO_X86)
    assert x86["model"].startswith("AMD Ryzen 9 7950X") and x86["logical"] == 3 and x86["physical"] == 2
    pi = H.parse_cpuinfo(CPUINFO_PI)
    assert pi["model"] == "Raspberry Pi 5 Model B Rev 1.0" and pi["logical"] == 2
    grav = H.parse_cpuinfo(CPUINFO_GRAVITON)
    assert grav["model"] == "Neoverse-V1" and grav["logical"] == 2
    assert H.parse_cpuinfo("CPU implementer : 0x61\nCPU part : 0x999\n")["model"] == "Apple CPU"
    assert H.parse_cpuinfo("")["model"] == ""


def test_parse_sysctl_and_vm_stat():
    sc = H.parse_sysctl(SYSCTL_MAC + "garbage line without separator\n")
    assert sc["machdep.cpu.brand_string"] == "Apple M3 Max" and sc["hw.memsize"] == "68719476736"
    assert H.parse_sysctl("hw.model = Intel Xeon\n") == {"hw.model": "Intel Xeon"}
    assert H.parse_vm_stat(VM_STAT) == round((100000 + 800000 + 50000) * 16384 / 1024 ** 3, 1)
    assert H.parse_vm_stat("") == 0.0


def test_parse_nvidia_smi():
    gpus = H.parse_nvidia_smi(NVIDIA_SMI + "\nNo devices were found\n")
    assert [g["name"] for g in gpus] == ["NVIDIA GeForce RTX 4090", "NVIDIA H100 80GB HBM3", "NVIDIA GB10"]
    assert gpus[0]["vram_gb"] == 24.0 and gpus[0]["driver"] == "550.54.14" and gpus[0]["api"] == "CUDA"
    assert gpus[1]["vram_gb"] == 79.6
    assert gpus[2]["vram_gb"] is None and gpus[2]["unified"] is True     # DGX Spark style unified memory
    assert H.parse_nvidia_smi("") == []


def test_parse_rocm_smi():
    gpus = H.parse_rocm_smi(ROCM_SMI)
    assert [g["name"] for g in gpus] == ["Radeon RX 7900 XTX", "AMD Instinct MI300X"]
    assert gpus[0]["vram_gb"] == 24.0 and gpus[1]["vram_gb"] == 192.0 and gpus[0]["api"] == "ROCm"
    assert H.parse_rocm_smi("not json") == [] and H.parse_rocm_smi("[1, 2]") == []


def test_parse_lspci_mm_skips_virtual_and_non_display():
    gpus = H.parse_lspci_mm(LSPCI_MM)
    names = [(g["vendor"], g["name"], g["virtual"]) for g in gpus]
    assert ("intel", "Iris Xe Graphics", False) in names
    assert ("nvidia", "GeForce RTX 4090", False) in names
    assert ("amd", "Radeon RX 7900 XT/7900 XTX/7900M", False) in names
    assert any(v for (_, n, v) in names if "ASPEED" in n) and any(v for (_, n, v) in names if "Basic Render" in n)
    assert not any("Audio" in n for (_, n, _) in names)
    merged = H.merge_gpus(gpus)
    assert {g["vendor"] for g in merged} == {"intel", "nvidia", "amd"}


def test_parse_system_profiler_apple_and_intel_mac():
    apple = H.parse_system_profiler(SP_APPLE)
    assert apple == [dict(apple[0], vendor="apple", name="Apple M3 Max", cores=40, unified=True, api="Metal")]
    assert apple[0]["vram_gb"] is None
    intel = H.parse_system_profiler(SP_INTEL_MAC)
    assert intel[0]["vendor"] == "intel" and intel[0]["vram_gb"] == 1.5
    assert intel[1]["vendor"] == "amd" and intel[1]["vram_gb"] == 8.0 and intel[1]["api"] == "Metal"
    assert H.parse_system_profiler("{}") == [] and H.parse_system_profiler("nope") == []


def test_nvidia_procfs_never_exposes_the_uuid():
    assert H.parse_nvidia_proc(NV_PROC) == "NVIDIA GeForce RTX 3090"
    assert H.parse_nvidia_version(NV_VERSION) == "550.54.14"
    assert H.parse_nvidia_version("") == ""


def test_detect_container():
    none = {"/.dockerenv": None, "/run/.containerenv": None, "/proc/1/cgroup": "0::/\n"}
    assert H.detect_container({}, none) == ""
    assert H.detect_container({}, dict(none, **{"/.dockerenv": ""})) == "docker"
    assert H.detect_container({}, dict(none, **{"/run/.containerenv": ""})) == "podman"
    assert H.detect_container({"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, none) == "kubernetes"
    assert H.detect_container({"container": "lxc"}, none) == "lxc"
    assert H.detect_container({}, dict(none, **{"/proc/1/cgroup": "12:pids:/docker/abc\n"})) == "docker"


def test_merge_prefers_vendor_tools_and_joins_sysfs_with_lspci():
    nv = H.parse_nvidia_smi("NVIDIA GeForce RTX 4090, 24564, 550.54.14\n")
    lspci = H.parse_lspci_mm(LSPCI_MM)
    merged = H.merge_gpus(nv, lspci)
    assert [g["name"] for g in merged if g["vendor"] == "nvidia"] == ["NVIDIA GeForce RTX 4090"]
    sysfs = [{"vendor": "amd", "name": "", "slot": "0000:03:00.0", "vram_gb": 24.0, "driver": "", "api": "ROCm",
              "unified": False, "source": "sysfs"}]
    amd = [g for g in H.merge_gpus(sysfs, lspci) if g["vendor"] == "amd"]
    assert len(amd) == 1 and amd[0]["vram_gb"] == 24.0 and "7900" in amd[0]["name"]


@pytest.mark.parametrize("raw,short", [
    ("Intel(R) Core(TM) i7-12700H", "Core i7-12700H"),
    ("AMD Ryzen 9 7950X 16-Core Processor", "Ryzen 9 7950X"),
    ("Intel(R) Xeon(R) Processor @ 2.10GHz", "Xeon"),
    ("Intel(R) Core(TM) i9-9880H CPU @ 2.30GHz", "Core i9-9880H"),
    ("AMD Ryzen 7 PRO 7840U w/ Radeon 780M Graphics", "Ryzen 7 PRO 7840U"),
    ("Apple M3 Max", "Apple M3 Max"),
    ("Snapdragon(R) X Elite - X1E78100 - Qualcomm(R) Oryon(TM) CPU", "Snapdragon X Elite X1E78100"),
])
def test_short_cpu(raw, short):
    assert H.short_cpu(raw) == short


@pytest.mark.parametrize("raw,short", [
    ("NVIDIA GeForce RTX 4090", "RTX 4090"),
    ("NVIDIA RTX A6000", "RTX A6000"),
    ("NVIDIA H100 80GB HBM3", "H100"),
    ("AMD Radeon RX 7900 XTX", "Radeon RX 7900 XTX"),
    ("Intel(R) Iris(R) Xe Graphics", "Iris Xe Graphics"),
    ("NVIDIA GeForce RTX 4070 Laptop GPU", "RTX 4070"),
])
def test_short_gpu(raw, short):
    assert H.short_gpu(raw) == short


APPLE = {"os": "darwin", "arch": "arm64", "chip": "Apple M3 Max", "cpu": "Apple M3 Max", "cores": 16,
         "physical_cores": 16, "ram_gb": 64.0, "unified": True, "env": {},
         "gpus": [{"vendor": "apple", "name": "Apple M3 Max", "cores": 40, "vram_gb": None, "api": "Metal",
                   "unified": True}]}
PLAIN = {"os": "linux", "arch": "x86_64", "cpu": "", "cores": 8, "ram_gb": 15.4, "gpus": [], "env": {}}
RIG = {"os": "linux", "arch": "x86_64", "cpu": "AMD Ryzen 9 7950X 16-Core Processor", "cores": 32,
       "physical_cores": 16, "ram_gb": 62.5, "env": {"wsl": True},
       "gpus": H.parse_nvidia_smi("NVIDIA GeForce RTX 4090, 24564, 550.54.14\n")}


def test_summary_matches_the_boot_row_and_fits_any_width():
    assert H.summary(APPLE) == "Apple M3 Max · 16 cores · 64 GB · Metal"
    assert H.summary(PLAIN) == "x86_64 · 8 cores · 15 GB · no GPU"
    full = H.summary(RIG, 80)
    assert full == "Ryzen 9 7950X · 16 cores · 62 GB · RTX 4090 24 GB · CUDA · WSL"
    for width in (60, 45, 40, 30, 20, 8, 1):
        s = H.summary(RIG, width)
        assert len(s) <= width, (width, s)
    assert "RTX 4090" in H.summary(RIG, 45)
    two = dict(RIG, gpus=RIG["gpus"] * 2)
    assert "2x RTX 4090" in H.summary(two, 80)


def test_fx_budget_levels():
    base = {"cores": 8, "ram_gb": 16.0, "env": {}}
    assert H.fx_budget(base, {}) == "full"
    assert H.fx_budget(base, {"PULSAR_FX": "lite"}) == "lite"
    assert H.fx_budget(base, {"PULSAR_FX": "off"}) == "minimal"
    assert H.fx_budget({"cores": 1}, {"PULSAR_FX": "full"}) == "full"
    assert H.fx_budget(base, {"PULSAR_REDUCED_MOTION": "1"}) == "minimal"
    assert H.fx_budget(base, {"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}) == "lite"
    assert H.fx_budget(dict(base, env={"ssh": True}), {}) == "lite"
    assert H.fx_budget(dict(base, cores=2), {}) == "lite"
    assert H.fx_budget(dict(base, ram_gb=3.0), {}) == "lite"
    assert H.fx_budget(base, {"TERM": "linux"}) == "lite"
    assert H.fx_budget(dict(base, env={"termux": True}), {}) == "lite"
    assert H.fx_budget(dict(base, cores=1), {}) == "minimal"
    assert H.fx_budget(dict(base, ram_gb=1.0), {}) == "minimal"


def test_local_model_hint():
    assert H.local_model_hint(dict(APPLE)) == "64 GB unified: 30B-class coders at 8-bit, 70B at 4-bit"
    rig = dict(RIG, vram_gb=24.0, unified=False)
    assert H.local_model_hint(rig) == "24 GB VRAM: 27-32B coders or 30B-A3B MoE"
    assert H.local_model_hint(dict(PLAIN, vram_gb=0)) == "15 GB RAM, CPU only: 7-9B coder models"
    assert H.local_model_hint({"ram_gb": 4.0}) == "4 GB RAM, CPU only: cloud models recommended"
    assert H.local_model_hint({"unified": True, "ram_gb": 512.0}) == "512 GB unified: 200B+ MoE models"
    assert H.local_model_hint({}).startswith("unknown hardware")


def test_which_never_resolves_into_the_current_directory(tmp_path, monkeypatch):
    fake = tmp_path / ("nvidia-smi.exe" if os.name == "nt" else "nvidia-smi")
    fake.write_text("#!/bin/sh\necho pwned\n")
    fake.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(tmp_path), os.environ.get("PATH", "")]))
    found = H.which("nvidia-smi")
    assert found is None or os.path.dirname(os.path.realpath(found)) != os.path.realpath(str(tmp_path))


def test_run_has_a_hard_timeout_and_never_raises():
    t0 = time.time()
    assert H.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3) is None
    assert time.time() - t0 < 2.0
    assert H.run(["definitely-not-a-real-tool-xyz"]) is None
    assert H.run([sys.executable, "-c", "print('hi')"]).strip() == "hi"


def test_child_env_drops_credentials(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    env = H._child_env()
    assert not {"OPENROUTER_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"} & set(env)
    assert env["LC_ALL"] == "C" and "PATH" in env


def _keys(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k).lower())
            _keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys(v, out)
    return out


def test_probe_live_is_fast_private_and_cached(pulsar_home):
    H.reset()
    pulsar_home.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    hw = H.probe(timeout=1.5)
    assert time.time() - t0 < 4.0
    for key in ("os", "arch", "cpu", "cores", "ram_gb", "gpus", "accel", "env", "terminal", "partial"):
        assert key in hw
    assert isinstance(hw["gpus"], list) and hw["cores"] >= 1
    assert not _keys(hw, set()) & {"hostname", "host", "user", "username", "serial", "uuid", "mac"}
    json.dumps(hw)                                       # plain data only
    t1 = time.time()
    again = H.probe()
    assert time.time() - t1 < 0.2 and again["cpu"] == hw["cpu"]
    if not hw["partial"]:
        cache = pulsar_home / "cache" / "hardware.json"
        assert cache.exists()
        H.reset()
        assert H.probe()["gpu_cache"] is True
    assert isinstance(H.summary(hw), str) and H.fx_budget(hw) in ("full", "lite", "minimal")
    assert ":" in H.local_model_hint(hw) or H.local_model_hint(hw).startswith("unknown")


def test_probe_does_not_create_a_pulsar_home(tmp_path, monkeypatch):
    H.reset()
    home = tmp_path / "nothere"
    monkeypatch.setenv("PULSAR_HOME", str(home))
    H.probe(timeout=1.0)
    assert not home.exists()
    H.reset()


def test_cache_path_follows_the_pulsar_home_rules(monkeypatch, tmp_path):
    default = tmp_path / "default-home"
    default.mkdir()
    real_expand = os.path.expanduser
    monkeypatch.setattr(H.os.path, "expanduser", lambda p: str(default) if p == "~/.pulsarcode" else real_expand(p))
    monkeypatch.setenv("PULSAR_HOME", "")                  # empty: the default home, as config.home() reads it
    assert H._cache_path() == os.path.join(str(default), "cache", "hardware.json")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "relhome").mkdir()
    monkeypatch.setenv("PULSAR_HOME", "relhome")           # relative: no home at all, never the current folder
    assert H._cache_path() is None


def test_peek_never_blocks():
    H.reset()
    t0 = time.time()
    p = H.peek()
    assert time.time() - t0 < 0.5 and p["cores"] >= 1 and "env" in p
    H.basics()
    assert "ram_gb" in H.peek()
    H.reset()


def test_a_late_gpu_probe_caches_where_the_probe_started(monkeypatch, tmp_path):
    # A GPU tool that outlives the probe's deadline finishes in the background and caches its answer. It
    # caches into the home the probe started with, never into whatever PULSAR_HOME names by then: another
    # home, or none at all, which is the default home of whoever is running.
    first, later = tmp_path / "first", tmp_path / "later"
    first.mkdir()
    later.mkdir()
    monkeypatch.setenv("PULSAR_HOME", str(first))
    gate = threading.Event()

    def slow_tool(argv, timeout=H.TIMEOUT):
        gate.wait(5)
        return ""

    monkeypatch.setattr(H, "run", slow_tool)
    hw = {"os": "darwin", "arch": "arm64", "cpu": "test", "ram_gb": 8, "version": "1", "env": {}}
    _, complete = H._slow_gpus(hw, 0.05)
    assert not complete
    monkeypatch.setenv("PULSAR_HOME", str(later))
    gate.set()
    first_cache, later_cache = first / "cache" / "hardware.json", later / "cache" / "hardware.json"
    deadline = time.time() + 5
    while time.time() < deadline and not (first_cache.exists() or later_cache.exists()):
        time.sleep(0.05)
    assert first_cache.exists() and not later_cache.exists()
