# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
"""What machine are we on? A fast, private, never-hanging hardware probe.

probe() returns one dict: OS and version, CPU, cores, memory, GPUs and the
compute API they speak (CUDA, ROCm, Metal, Vulkan), the terminal, and the
environment (container, WSL, Termux, SSH, CI). It feeds three things:

  summary()           the HARDWARE row of the boot screen
  fx_budget()         how much animation this machine and link can carry
  local_model_hint()  which local model size class fits in memory

Rules this module keeps:

* Fast. Files under /proc and /sys, the Windows registry and one sysctl
  call answer most questions in a few milliseconds. Subprocesses
  (nvidia-smi, rocm-smi, lspci, system_profiler, sysctl, vm_stat) run in
  parallel threads, each with a 1.5 s timeout, and the whole probe has a
  deadline, so a wedged GPU driver can never hang a launch. GPU results
  are cached on disk for a few days, so later launches skip them.
* Private. Nothing leaves the machine. The result holds no hostname,
  user name, serial number, MAC address or GPU UUID, and child processes
  get an environment with anything that looks like a credential removed.
* Safe. Tools are looked up on absolute PATH entries only (never the
  current directory), never through a shell.

Every parser is a pure function over captured text and is unit-tested.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

TIMEOUT = 1.5            # per subprocess
DEADLINE = 1.8           # whole probe
CACHE_TTL = 3 * 86400    # GPU facts are stable; drivers and cards change rarely
CACHE_VERSION = 2

_LOCK = threading.Lock()
_BASICS: Optional[Dict[str, Any]] = None
_FULL: Optional[Dict[str, Any]] = None

GIB = 1024.0 ** 3


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _read(path: str, limit: int = 262144) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""


def _gb(n_bytes: float) -> float:
    return round(float(n_bytes) / GIB, 1)


_SECRETISH = re.compile(r"(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I)


def _child_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _SECRETISH.search(k)}
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def _search_dirs() -> List[str]:
    """Absolute PATH entries plus the usual tool homes; never the current directory."""
    try:
        cwd = os.path.realpath(os.getcwd())
    except OSError:
        cwd = ""
    extra: Tuple[str, ...] = ()
    if sys.platform == "darwin":
        extra = ("/usr/sbin", "/usr/bin", "/sbin")
    elif os.name == "nt":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        extra = (os.path.join(root, "System32"),)
    else:
        extra = ("/usr/bin", "/usr/sbin", "/bin", "/sbin", "/usr/local/bin", "/usr/lib/wsl/lib", "/opt/rocm/bin")
    out: List[str] = []
    for d in os.environ.get("PATH", "").split(os.pathsep) + list(extra):
        if not d or not os.path.isabs(d) or d in out:
            continue
        try:
            if cwd and os.path.realpath(d) == cwd:
                continue
        except OSError:
            continue
        out.append(d)
    return out


def which(name: str) -> Optional[str]:
    """Like shutil.which, but only absolute PATH entries (Windows' implicit cwd lookup is skipped)."""
    exts = [""]
    if os.name == "nt":
        exts = [e.lower() for e in (os.environ.get("PATHEXT") or ".EXE;.BAT;.CMD").split(";") if e] + [""]
    for d in _search_dirs():
        for ext in exts:
            p = os.path.join(d, name + ext)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def run(argv: Sequence[str], timeout: float = TIMEOUT) -> Optional[str]:
    """stdout of a tool, or None. Runs in a daemon thread with a hard timeout; never raises."""
    exe = which(argv[0]) if not os.path.isabs(argv[0]) else (argv[0] if os.path.isfile(argv[0]) else None)
    if not exe:
        return None
    box: Dict[str, str] = {}

    def target() -> None:
        try:
            import subprocess
            kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                                  "stderr": subprocess.DEVNULL, "timeout": timeout, "env": _child_env()}
            if os.name == "nt":
                kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console flash
            proc = subprocess.run([exe] + list(argv[1:]), **kw)
            if proc.stdout:
                box["out"] = proc.stdout[:1_000_000].decode("utf-8", "replace")
        except Exception:
            pass

    th = threading.Thread(target=target, name="pulsar-hw", daemon=True)
    th.start()
    th.join(timeout + 0.3)
    return box.get("out")


def _parallel(jobs: Mapping[str, Callable[[], Any]], deadline: float,
              on_complete: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    """Run callables concurrently; return what finished before the deadline.

    When the deadline cuts some jobs short and on_complete is given, a daemon thread waits for
    the stragglers (each is itself bounded by TIMEOUT) and hands it the full results, so slow
    facts still land in the cache for next time without anyone waiting on them now.
    """
    results: Dict[str, Any] = {}
    lock = threading.Lock()

    def wrap(name: str, fn: Callable[[], Any]) -> Callable[[], None]:
        def go() -> None:
            try:
                val = fn()
            except Exception:
                val = None
            with lock:
                results[name] = val
        return go

    threads = []
    for name, fn in jobs.items():
        th = threading.Thread(target=wrap(name, fn), name="pulsar-hw-" + name, daemon=True)
        th.start()
        threads.append(th)
    end = time.time() + deadline
    for th in threads:
        th.join(max(0.0, end - time.time()))
    with lock:
        snap = dict(results)
    if on_complete is not None and len(snap) < len(jobs):
        def finish() -> None:
            for th in threads:
                th.join(TIMEOUT + 0.5)
            with lock:
                full = dict(results)
            if len(full) == len(jobs):
                try:
                    on_complete(full)
                except Exception:
                    pass
        threading.Thread(target=finish, name="pulsar-hw-finish", daemon=True).start()
    return snap


# ---------------------------------------------------------------------------
# Pure parsers (unit-tested with captured outputs)
# ---------------------------------------------------------------------------

def parse_os_release(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v.replace('\\"', '"').replace("\\$", "$").replace("\\\\", "\\")
    return out


def parse_meminfo(text: str) -> Tuple[float, float]:
    """(total GiB, available GiB) from /proc/meminfo."""
    vals: Dict[str, float] = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)\s*kB", line)
        if m:
            vals[m.group(1)] = float(m.group(2)) * 1024
    total = vals.get("MemTotal", 0.0)
    avail = vals.get("MemAvailable")
    if avail is None:
        avail = vals.get("MemFree", 0.0) + vals.get("Buffers", 0.0) + vals.get("Cached", 0.0)
    return _gb(total), _gb(avail)


_ARM_VENDORS = {"0x41": "ARM", "0x42": "Broadcom", "0x48": "HiSilicon", "0x4e": "NVIDIA", "0x51": "Qualcomm",
                "0x53": "Samsung", "0x61": "Apple", "0x6d": "Microsoft", "0xc0": "Ampere"}
_ARM_PARTS = {"0xd03": "Cortex-A53", "0xd04": "Cortex-A35", "0xd05": "Cortex-A55", "0xd07": "Cortex-A57",
              "0xd08": "Cortex-A72", "0xd09": "Cortex-A73", "0xd0a": "Cortex-A75", "0xd0b": "Cortex-A76",
              "0xd0c": "Neoverse-N1", "0xd0d": "Cortex-A77", "0xd40": "Neoverse-V1", "0xd41": "Cortex-A78",
              "0xd44": "Cortex-X1", "0xd46": "Cortex-A510", "0xd47": "Cortex-A710", "0xd48": "Cortex-X2",
              "0xd49": "Neoverse-N2", "0xd4d": "Cortex-A715", "0xd4e": "Cortex-X3", "0xd4f": "Neoverse-V2",
              "0xd80": "Cortex-A520", "0xd81": "Cortex-A720", "0xd82": "Cortex-X4", "0xd84": "Neoverse-V3",
              "0xd8e": "Neoverse-N3"}


def parse_cpuinfo(text: str) -> Dict[str, Any]:
    """CPU model, logical count and (when the kernel reports topology) physical core count."""
    model = ""
    hardware = ""
    logical = 0
    cores_per_pkg: Dict[str, int] = {}
    phys_ids = set()
    pairs = set()
    cur_phys = None
    impl = part = ""
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = [s.strip() for s in line.split(":", 1)]
        kl = k.lower()
        if kl == "processor" and v.isdigit():
            logical += 1
        elif kl in ("model name", "cpu model") and not model:
            model = v
        elif kl == "hardware" and not hardware:
            hardware = v
        elif kl == "model" and not model and not v.isdigit():
            model = v                                   # Raspberry Pi and some ARM boards
        elif kl == "physical id":
            cur_phys = v
            phys_ids.add(v)
        elif kl == "core id" and cur_phys is not None:
            pairs.add((cur_phys, v))
        elif kl == "cpu cores" and cur_phys is not None and v.isdigit():
            cores_per_pkg[cur_phys] = int(v)
        elif kl == "cpu implementer" and not impl:
            impl = v.lower()
        elif kl == "cpu part" and not part:
            part = v.lower()
    if not model:
        name = _ARM_PARTS.get(part, "")
        vendor = _ARM_VENDORS.get(impl, "")
        if name:
            model = name if vendor in ("", "ARM") else vendor + " " + name
        else:
            model = hardware or (vendor + " CPU" if vendor else "")
    physical = len(pairs) or sum(cores_per_pkg.values()) or 0
    return {"model": model, "logical": logical, "physical": physical}


def parse_sysctl(text: str) -> Dict[str, str]:
    """`sysctl a b c` output ("key: value" on macOS/BSD, "key = value" on Linux)."""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([\w.]+)\s*[:=]\s*(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def parse_vm_stat(text: str) -> float:
    """Available memory in GiB from macOS vm_stat (free + inactive + speculative pages)."""
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else 4096
    pages = 0
    for key in ("Pages free", "Pages inactive", "Pages speculative"):
        mm = re.search(re.escape(key) + r":\s+(\d+)", text)
        if mm:
            pages += int(mm.group(1))
    return _gb(pages * page) if pages else 0.0


def _num(s: Any) -> Optional[float]:
    try:
        v = float(str(s).strip())
    except (TypeError, ValueError):
        return None
    return v if v == v and v >= 0 else None


def parse_nvidia_smi(text: str) -> List[Dict[str, Any]]:
    """`nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits`."""
    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0] or parts[0].lower().startswith(("name", "no devices", "nvidia-smi has failed")):
            continue
        name = parts[0]
        mib = _num(parts[1]) if len(parts) > 1 else None
        drv = parts[2] if len(parts) > 2 and re.match(r"^[\d.]+$", parts[2]) else ""
        unified = mib is None and bool(re.search(r"\b(GB10|Thor|Orin|Xavier|Jetson|Tegra)\b", name, re.I))
        gpus.append({"vendor": "nvidia", "name": name, "vram_gb": round(mib / 1024.0, 1) if mib else None,
                     "driver": drv, "api": "CUDA", "unified": unified, "source": "nvidia-smi"})
    return gpus


def parse_rocm_smi(text: str) -> List[Dict[str, Any]]:
    """`rocm-smi --showproductname --showmeminfo vram --json` (key spellings vary by release)."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    gpus = []
    for card, info in sorted(data.items()):
        if not str(card).lower().startswith("card") or not isinstance(info, dict):
            continue
        low = {str(k).lower(): v for k, v in info.items()}
        name = ""
        for k in ("card series", "marketing name", "card model", "device name"):
            v = str(low.get(k) or "").strip()
            if v and not re.match(r"^(0x[0-9a-f]+|n/a)$", v, re.I):
                name = v
                break
        vram = None
        for k, v in low.items():
            if "vram total memory" in k and "used" not in k:
                b = _num(v)
                if b:
                    vram = _gb(b)
        gpus.append({"vendor": "amd", "name": name or "AMD GPU", "vram_gb": vram, "driver": "", "api": "ROCm",
                     "unified": False, "source": "rocm-smi"})
    return gpus


_PCI_DISPLAY = ("vga compatible controller", "3d controller", "display controller")
# Display adapters that cannot run a model: BMC framebuffers and virtual GPUs.
_NOT_COMPUTE = re.compile(r"aspeed|matrox|cirrus|vmware|virtio|qxl|bochs|red hat|microsoft|hyper-v|"
                          r"basic (render|display)|remote display|parsec|silicon motion|xgi|innotek|virtualbox", re.I)


def _vendor_of(text: str) -> str:
    t = text.lower()
    if "nvidia" in t:
        return "nvidia"
    if "advanced micro devices" in t or "amd" in t or "ati " in t or t.startswith("ati") or "radeon" in t:
        return "amd"
    if "intel" in t:
        return "intel"
    if "apple" in t:
        return "apple"
    if "qualcomm" in t or "adreno" in t:
        return "qualcomm"
    if "moore threads" in t:
        return "mthreads"
    return "other"


def parse_lspci_mm(text: str) -> List[Dict[str, Any]]:
    """`lspci -mm` lines for display-class devices -> [{vendor, name, slot, virtual}]."""
    import shlex
    gpus = []
    for line in text.splitlines():
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) < 4 or parts[1].lower() not in _PCI_DISPLAY:
            continue
        slot, _, vendor_s, device = parts[0], parts[1], parts[2], parts[3]
        m = re.search(r"\[([^\]]+)\]\s*$", device)
        name = m.group(1) if m else device
        vendor = _vendor_of(vendor_s)
        if vendor == "amd" and not name.lower().startswith(("radeon", "instinct", "amd", "firepro")):
            name = "Radeon " + name if "/" not in name else name
        virtual = bool(_NOT_COMPUTE.search(vendor_s + " " + device))
        gpus.append({"vendor": vendor, "name": name.strip(), "slot": slot, "vram_gb": None, "driver": "",
                     "api": _api_for(vendor), "unified": False, "virtual": virtual, "source": "lspci"})
    return gpus


def _vram_text(s: str) -> Optional[float]:
    m = re.match(r"^\s*([\d.]+)\s*(GB|MB|TB)", s or "", re.I)
    if not m:
        return None
    v = float(m.group(1))
    unit = m.group(2).upper()
    return round(v / 1024.0, 1) if unit == "MB" else round(v * 1024.0, 1) if unit == "TB" else round(v, 1)


def parse_system_profiler(text: str) -> List[Dict[str, Any]]:
    """`system_profiler SPDisplaysDataType -json` -> GPUs (Apple Silicon: cores, Intel Macs: VRAM)."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    gpus = []
    for item in (data.get("SPDisplaysDataType") or []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("sppci_model") or item.get("_name") or "").strip()
        vendor_s = str(item.get("spdisplays_vendor") or item.get("sppci_vendor") or "")
        vendor = _vendor_of(vendor_s + " " + name)
        cores = str(item.get("sppci_cores") or "").strip()
        vram = _vram_text(str(item.get("spdisplays_vram") or item.get("_spdisplays_vram") or ""))
        unified = vendor == "apple" or name.startswith("Apple")
        metal = str(item.get("spdisplays_mtlgpufamilysupport") or item.get("spdisplays_metal") or "")
        gpus.append({"vendor": "apple" if unified else vendor, "name": name or "GPU",
                     "cores": int(cores) if cores.isdigit() else None, "vram_gb": None if unified else vram,
                     "driver": "", "api": "Metal" if (unified or metal or vendor == "amd") else _api_for(vendor),
                     "unified": unified, "source": "system_profiler"})
    return gpus


def parse_nvidia_proc(text: str) -> str:
    """GPU model from /proc/driver/nvidia/gpus/*/information (the UUID line is ignored on purpose)."""
    m = re.search(r"^Model:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else ""


def parse_nvidia_version(text: str) -> str:
    m = re.search(r"Kernel Module(?:\s+for\s+\S+)?\s+([\d.]+)", text)
    return m.group(1) if m else ""


def _api_for(vendor: str) -> str:
    return {"nvidia": "CUDA", "amd": "ROCm", "apple": "Metal", "intel": "Vulkan", "qualcomm": "Vulkan"}.get(vendor, "")


def detect_container(env: Mapping[str, str], files: Mapping[str, Optional[str]]) -> str:
    """'docker', 'podman', 'kubernetes', 'lxc', 'container' (unknown runtime) or ''.

    files maps a few paths to their contents, None when the file is missing:
    /.dockerenv, /run/.containerenv and /proc/1/cgroup.
    """
    if env.get("KUBERNETES_SERVICE_HOST"):
        return "kubernetes"
    c = (env.get("container") or "").strip().lower()
    if c:
        return {"oci": "container", "systemd-nspawn": "nspawn"}.get(c, c)
    if files.get("/run/.containerenv") is not None:
        return "podman"
    if files.get("/.dockerenv") is not None:
        return "docker"
    cg = files.get("/proc/1/cgroup") or ""
    for needle, name in (("kubepods", "kubernetes"), ("docker", "docker"), ("libpod", "podman"),
                         ("lxc", "lxc"), ("containerd", "container")):
        if needle in cg:
            return name
    return ""


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def short_cpu(model: str) -> str:
    s = model or ""
    s = re.sub(r"\((R|TM|tm|r)\)|[\u00ae\u2122]", "", s)
    s = re.sub(r"\s*@\s*[\d.]+\s*[GM]Hz", "", s)
    s = re.sub(r"\b\d+-Core\b", "", s)
    s = re.sub(r"\bwith Radeon.*$|\bw/ Radeon.*$", "", s, flags=re.I)
    s = re.sub(r"\b(CPU|Processor|Core Processor)\b", "", s)
    s = re.sub(r"^(Intel|AMD)\s+(?=(Core|Xeon|Ryzen|EPYC|Threadripper|Celeron|Pentium|Atom|Athlon|Ultra))", "", s)
    s = re.sub(r"\s+-\s+", " ", s)
    s = re.sub(r"Qualcomm\s+Oryon", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -")
    return s


def short_gpu(name: str) -> str:
    s = re.sub(r"\((R|TM|tm)\)|[\u00ae\u2122]", "", name or "")
    s = re.sub(r"^NVIDIA\s+(GeForce\s+)?", "", s)
    s = re.sub(r"^GeForce\s+", "", s)
    s = re.sub(r"^(AMD|ATI)\s+", "", s)
    s = re.sub(r"^Intel\s+(Corporation\s+)?", "", s)
    s = re.sub(r"\s+\d+\s*GB\b.*$", "", s)                  # "H100 80GB HBM3" -> "H100"
    s = re.sub(r"\s+(Laptop GPU|Graphics Processor)$", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s or (name or "GPU")


def _fmt_gb(v: Optional[float]) -> str:
    if not v:
        return ""
    return "%d GB" % int(round(v)) if v >= 1.5 else "%d MB" % int(round(v * 1024))


# ---------------------------------------------------------------------------
# Probes per platform
# ---------------------------------------------------------------------------

def _arch() -> str:
    m = (platform.machine() or "").lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64", "armv8l": "arm64"}.get(m, m or "unknown")


def _terminal(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    env = env if env is not None else os.environ
    try:
        from pulsar.term import detect_color, detect_unicode
        tty = sys.stderr.isatty()
        color = detect_color(env, tty)
        uni = detect_unicode(env, getattr(sys.stderr, "encoding", "") or "")
    except Exception:
        color, uni, tty = 0, False, False
    cols = rows = 0
    for stream in (sys.stderr, sys.stdout, sys.stdin):
        try:
            s = os.get_terminal_size(stream.fileno())
            cols, rows = s.columns, s.lines
            break
        except (AttributeError, OSError, ValueError):
            continue
    mux = "tmux" if env.get("TMUX") else "screen" if env.get("STY") else "zellij" if env.get("ZELLIJ") else ""
    program = env.get("TERM_PROGRAM") or ("Windows Terminal" if env.get("WT_SESSION") else
                                          "kitty" if env.get("KITTY_WINDOW_ID") else
                                          "Konsole" if env.get("KONSOLE_VERSION") else "")
    if os.name == "nt" and not program and not env.get("TERM"):
        program = "conhost"
    return {"program": program, "version": env.get("TERM_PROGRAM_VERSION", ""), "term": env.get("TERM", ""),
            "cols": cols, "rows": rows, "color": color, "unicode": uni, "tty": tty, "multiplexer": mux}


def _env_flags(env: Mapping[str, str], os_name: str) -> Dict[str, Any]:
    flags: Dict[str, Any] = {"container": "", "wsl": False, "termux": False, "ssh": False, "ci": bool(env.get("CI"))}
    flags["ssh"] = bool(env.get("SSH_CONNECTION") or env.get("SSH_CLIENT") or env.get("SSH_TTY"))
    flags["termux"] = "com.termux" in (env.get("PREFIX") or "") or bool(env.get("TERMUX_VERSION"))
    if os_name == "linux":
        ver = _read("/proc/version", 4096).lower()
        flags["wsl"] = bool(env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP")) or "microsoft" in ver or "wsl" in ver
        files: Dict[str, Optional[str]] = {}
        for p in ("/.dockerenv", "/run/.containerenv"):
            files[p] = "" if os.path.exists(p) else None
        files["/proc/1/cgroup"] = _read("/proc/1/cgroup", 16384)
        flags["container"] = detect_container(env, files)
    return flags


def _linux_basics(hw: Dict[str, Any]) -> None:
    rel = parse_os_release(_read("/etc/os-release") or _read("/usr/lib/os-release"))
    hw["distro"] = rel.get("ID", "")
    hw["version"] = rel.get("VERSION_ID", "")
    hw["os_name"] = rel.get("PRETTY_NAME") or rel.get("NAME") or "Linux"
    cpu = parse_cpuinfo(_read("/proc/cpuinfo", 1 << 20))
    hw["cpu"] = cpu["model"]
    hw["physical_cores"] = cpu["physical"] or None
    total, avail = parse_meminfo(_read("/proc/meminfo"))
    hw["ram_gb"], hw["ram_avail_gb"] = total, avail
    limit = _read("/sys/fs/cgroup/memory.max").strip() or _read("/sys/fs/cgroup/memory/memory.limit_in_bytes").strip()
    if limit.isdigit() and 0 < int(limit) < total * GIB * 0.98:
        hw["ram_limit_gb"] = _gb(int(limit))
    model = _read("/proc/device-tree/model", 512).replace("\0", "").strip()
    if model:
        hw["board"] = model
    if hw["env"]["termux"] or os.path.exists("/system/build.prop"):
        hw["os"] = "android"
        hw["os_name"] = "Android" + (" (Termux)" if hw["env"]["termux"] else "")


def _darwin_basics(hw: Dict[str, Any]) -> None:
    ver = platform.mac_ver()[0]
    hw["distro"] = "macos"
    hw["version"] = ver
    hw["os_name"] = ("macOS " + ver).strip()
    keys = ["machdep.cpu.brand_string", "hw.memsize", "hw.ncpu", "hw.physicalcpu", "hw.perflevel0.physicalcpu",
            "hw.perflevel1.physicalcpu", "sysctl.proc_translated", "hw.optional.arm64"]
    got = _parallel({"sysctl": lambda: run(["sysctl"] + keys), "vm": lambda: run(["vm_stat"])}, TIMEOUT + 0.2)
    sc = parse_sysctl(got.get("sysctl") or "")
    brand = sc.get("machdep.cpu.brand_string", "")
    hw["cpu"] = brand
    mem = _num(sc.get("hw.memsize"))
    if mem:
        hw["ram_gb"] = _gb(mem)
    phys = _num(sc.get("hw.physicalcpu"))
    hw["physical_cores"] = int(phys) if phys else None
    p0, p1 = _num(sc.get("hw.perflevel0.physicalcpu")), _num(sc.get("hw.perflevel1.physicalcpu"))
    if p0 and p1:
        hw["core_split"] = "%dP+%dE" % (p0, p1)
    avail = parse_vm_stat(got.get("vm") or "")
    if avail:
        hw["ram_avail_gb"] = avail
    if brand.startswith("Apple") or sc.get("hw.optional.arm64") == "1":
        hw["chip"] = brand or "Apple Silicon"
        hw["unified"] = True
        hw["arch"] = "arm64"                     # also true under Rosetta, where machine() says x86_64
        if sc.get("sysctl.proc_translated") == "1":
            hw["rosetta"] = True


def _windows_basics(hw: Dict[str, Any]) -> None:
    ver = platform.version() or ""
    build = 0
    m = re.match(r"^\d+\.\d+\.(\d+)", ver)
    if m:
        build = int(m.group(1))
    rel = platform.release() or ""
    if rel == "10" and build >= 22000:
        rel = "11"
    hw["distro"] = "windows"
    hw["version"] = ver
    hw["os_name"] = ("Windows %s" % rel).strip()
    try:
        import winreg  # type: ignore[import-not-found]
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            hw["cpu"] = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except Exception:
        hw["cpu"] = platform.processor() or ""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
            hw["ram_gb"] = _gb(st.ullTotalPhys)
            hw["ram_avail_gb"] = _gb(st.ullAvailPhys)
    except Exception:
        pass


def _windows_gpus() -> List[Dict[str, Any]]:
    gpus: List[Dict[str, Any]] = []
    try:
        import winreg  # type: ignore[import-not-found]
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            for i in range(32):
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as k:
                        name = str(winreg.QueryValueEx(k, "DriverDesc")[0]).strip()
                        vram = None
                        for val in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                            try:
                                raw = winreg.QueryValueEx(k, val)[0]
                                if isinstance(raw, bytes):
                                    raw = int.from_bytes(raw[:8], "little")
                                if raw:
                                    vram = _gb(int(raw))
                                    break
                            except OSError:
                                continue
                except OSError:
                    continue
                vendor = _vendor_of(name)
                gpus.append({"vendor": vendor, "name": name, "vram_gb": vram, "driver": "", "api": _api_for(vendor),
                             "unified": False, "virtual": bool(_NOT_COMPUTE.search(name)), "source": "registry"})
    except Exception:
        pass
    return gpus


def _linux_fast_gpus() -> List[Dict[str, Any]]:
    """GPUs visible without running anything: the NVIDIA proc tree and DRM sysfs."""
    gpus: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir("/proc/driver/nvidia/gpus"))
    except OSError:
        names = []
    drv = parse_nvidia_version(_read("/proc/driver/nvidia/version", 4096))
    for n in names:
        model = parse_nvidia_proc(_read(os.path.join("/proc/driver/nvidia/gpus", n, "information"), 8192))
        if model:
            gpus.append({"vendor": "nvidia", "name": model, "slot": n, "vram_gb": None, "driver": drv, "api": "CUDA",
                         "unified": False, "source": "procfs"})
    try:
        cards = sorted(c for c in os.listdir("/sys/class/drm") if re.match(r"^card\d+$", c))
    except OSError:
        cards = []
    for c in cards:
        dev = os.path.join("/sys/class/drm", c, "device")
        vendor_id = _read(os.path.join(dev, "vendor"), 64).strip().lower()
        vendor = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel", "0x106b": "apple"}.get(vendor_id, "")
        if not vendor or (vendor == "nvidia" and gpus):
            continue
        vram_b = _num(_read(os.path.join(dev, "mem_info_vram_total"), 64))
        try:
            slot = os.path.basename(os.path.realpath(dev))
        except OSError:
            slot = ""
        gpus.append({"vendor": vendor, "name": "", "slot": slot, "vram_gb": _gb(vram_b) if vram_b else None,
                     "driver": "", "api": _api_for(vendor), "unified": False, "source": "sysfs"})
    return gpus


def _slot_tail(slot: str) -> str:
    return (slot or "").lower().split(":", 1)[-1] if (slot or "").count(":") >= 2 else (slot or "").lower()


def merge_gpus(*groups: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Combine GPU lists from several sources; earlier groups are more authoritative."""
    out: List[Dict[str, Any]] = []
    for group in groups:
        for g in group:
            if g.get("virtual"):
                continue
            match = None
            for o in out:
                if g.get("slot") and o.get("slot") and _slot_tail(g["slot"]) == _slot_tail(o["slot"]):
                    match = o
                    break
            if match is None and g.get("source") in ("lspci", "sysfs", "procfs", "registry"):
                # A vendor-tool row already covers this vendor: fill gaps, don't duplicate.
                same = [o for o in out if o["vendor"] == g["vendor"] and o.get("source") in ("nvidia-smi", "rocm-smi")]
                if same:
                    continue
                if g.get("source") == "sysfs":
                    unnamed = [o for o in out if o["vendor"] == g["vendor"] and not o.get("vram_gb")]
                    if unnamed:
                        match = unnamed[0]
            if match is not None:
                for k, v in g.items():
                    if v and not match.get(k):
                        match[k] = v
                continue
            out.append(dict(g))
    for g in out:
        if not g.get("name"):
            g["name"] = {"amd": "AMD GPU", "intel": "Intel GPU", "nvidia": "NVIDIA GPU"}.get(g["vendor"], "GPU")
    return out


# ---------------------------------------------------------------------------
# Disk cache for the slow part
# ---------------------------------------------------------------------------

def _cache_path() -> Optional[str]:
    # As config.home() reads it: an empty PULSAR_HOME means the default; a relative one names no home at all.
    home = os.path.expanduser(os.environ.get("PULSAR_HOME") or "~/.pulsarcode")
    if not os.path.isabs(home) or not os.path.isdir(home):
        return None                     # never create a Pulsar home just to cache this, nor write into the cwd
    return os.path.join(home, "cache", "hardware.json")


def _fingerprint(hw: Mapping[str, Any]) -> str:
    drv = parse_nvidia_version(_read("/proc/driver/nvidia/version", 4096)) if hw.get("os") == "linux" else ""
    return "|".join(str(x) for x in (CACHE_VERSION, hw.get("os"), hw.get("arch"), hw.get("cpu"),
                                     int(round(hw.get("ram_gb") or 0)), hw.get("version"), drv,
                                     hw.get("env", {}).get("wsl"), hw.get("env", {}).get("container")))


def _cache_load(hw: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
    path = _cache_path()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("fp") != _fingerprint(hw):
        return None
    if not (0 <= time.time() - float(data.get("ts") or 0) < CACHE_TTL):
        return None
    gpus = data.get("gpus")
    if not isinstance(gpus, list) or not all(isinstance(g, dict) for g in gpus):
        return None
    return gpus


def _cache_save(hw: Mapping[str, Any], gpus: List[Dict[str, Any]], path: Optional[str] = None) -> None:
    """Write the GPU cache to `path` (where the probe that found `gpus` started), else to the home now."""
    path = path or _cache_path()
    if not path:
        return
    import tempfile
    tmp = ""
    folder = os.path.dirname(path)
    try:  # a launch that exec()s Claude Code mid-write can strand a temp file: sweep old ones
        now = time.time()
        for name in os.listdir(folder) if os.path.isdir(folder) else ():
            if name.startswith(".hardware.") and now - os.path.getmtime(os.path.join(folder, name)) > 60:
                os.unlink(os.path.join(folder, name))
    except OSError:
        pass
    try:
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".hardware.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"fp": _fingerprint(hw), "ts": time.time(), "gpus": gpus}, fh)
        os.replace(tmp, path)
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def basics(refresh: bool = False) -> Dict[str, Any]:
    """The cheap part: OS, CPU, cores, memory, environment, terminal. No GPU tools are run."""
    global _BASICS
    with _LOCK:
        if _BASICS is not None and not refresh:
            out = dict(_BASICS)
            out["terminal"] = _terminal()
            return out
    t0 = time.time()
    env = os.environ
    sysname = sys.platform
    os_name = "darwin" if sysname == "darwin" else "windows" if os.name == "nt" else \
        "linux" if sysname.startswith("linux") else "freebsd" if "bsd" in sysname else sysname
    hw: Dict[str, Any] = {"os": os_name, "os_name": platform.system() or os_name, "distro": "", "version": "",
                          "kernel": platform.release() if os_name != "windows" else "", "arch": _arch(),
                          "cpu": "", "cores": os.cpu_count() or 1, "physical_cores": None, "ram_gb": 0.0,
                          "ram_avail_gb": 0.0, "unified": False, "chip": "", "board": "",
                          "env": _env_flags(env, os_name)}
    try:
        aff = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
        if 0 < aff < hw["cores"]:
            hw["cores"] = aff                # containers and taskset limit what we can use
    except (AttributeError, OSError):
        pass
    try:
        if os_name in ("linux", "android"):
            _linux_basics(hw)
        elif os_name == "darwin":
            _darwin_basics(hw)
        elif os_name == "windows":
            _windows_basics(hw)
        elif os_name == "freebsd":
            sc = parse_sysctl(run(["sysctl", "hw.model", "hw.physmem"]) or "")
            hw["cpu"] = sc.get("hw.model", "")
            mem = _num(sc.get("hw.physmem"))
            hw["ram_gb"] = _gb(mem) if mem else 0.0
    except Exception:
        pass
    if not hw["cpu"]:
        hw["cpu"] = platform.processor() or ""
    hw["basics_ms"] = round((time.time() - t0) * 1000, 1)
    with _LOCK:
        _BASICS = dict(hw)
    hw["terminal"] = _terminal()
    return hw


def peek() -> Dict[str, Any]:
    """Whatever is already known, without probing anything slow: cached basics if a probe ran in
    this process, else core count and environment flags only. Never blocks."""
    with _LOCK:
        if _FULL is not None:
            return dict(_FULL)
        if _BASICS is not None:
            return dict(_BASICS)
    os_name = "darwin" if sys.platform == "darwin" else "windows" if os.name == "nt" else \
        "linux" if sys.platform.startswith("linux") else sys.platform
    cores = os.cpu_count() or 1
    return {"os": os_name, "cores": cores, "env": _env_flags(os.environ, os_name) if os_name != "linux" else
            {"ssh": bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY")),
             "termux": "com.termux" in (os.environ.get("PREFIX") or "")}}


def _slow_gpus(hw: Mapping[str, Any], deadline: float) -> Tuple[List[Dict[str, Any]], bool]:
    """(gpus, complete) from vendor tools; complete is False when the deadline cut a probe short
    (the stragglers then finish in the background and refresh the disk cache)."""
    os_name = hw.get("os")
    jobs: Dict[str, Callable[[], Any]] = {}
    if os_name in ("linux", "windows", "android"):
        if which("nvidia-smi"):
            jobs["nvidia"] = lambda: run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                                          "--format=csv,noheader,nounits"])
    if os_name == "linux":
        if which("rocm-smi"):
            jobs["rocm"] = lambda: run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"])
        if which("lspci"):
            jobs["lspci"] = lambda: run(["lspci", "-mm"])
    if os_name == "darwin":
        jobs["sp"] = lambda: run(["system_profiler", "-json", "-detailLevel", "mini", "SPDisplaysDataType"])
    if os_name == "windows":
        jobs["reg"] = _windows_gpus

    def merged(got: Mapping[str, Any]) -> List[Dict[str, Any]]:
        fast = _linux_fast_gpus() if os_name in ("linux", "android") else []
        return merge_gpus(parse_nvidia_smi(got.get("nvidia") or ""), parse_rocm_smi(got.get("rocm") or ""),
                          parse_system_profiler(got.get("sp") or ""), fast, parse_lspci_mm(got.get("lspci") or ""),
                          got.get("reg") or [])

    snapshot = dict(hw)
    # Resolved as the probe starts: a straggler that finishes later caches here, never where PULSAR_HOME
    # happens to point by then (another home, or none at all, which would mean the default one).
    cache_at = _cache_path()

    def late(full: Dict[str, Any]) -> None:
        if cache_at:
            _cache_save(snapshot, merged(full), cache_at)

    got = _parallel(jobs, deadline, on_complete=late) if jobs else {}
    return merged(got), all(k in got for k in jobs)


def _finish(hw: Dict[str, Any]) -> Dict[str, Any]:
    gpus = hw.get("gpus") or []
    board = (hw.get("board") or "").lower()
    if hw.get("chip") and not gpus:
        gpus = [{"vendor": "apple", "name": hw["chip"], "vram_gb": None, "api": "Metal", "unified": True,
                 "source": "chip"}]
    if "jetson" in board or "orin" in board:
        for g in gpus:
            if g["vendor"] == "nvidia":
                g["unified"] = True
        if not gpus:
            gpus = [{"vendor": "nvidia", "name": hw.get("board") or "Jetson", "vram_gb": None, "api": "CUDA",
                     "unified": True, "source": "board"}]
    hw["gpus"] = gpus
    discrete = [g for g in gpus if not g.get("unified") and (g.get("vram_gb") or 0) > 0]
    best = None
    for vendor in ("nvidia", "amd", "apple", "intel", "qualcomm"):
        cand = [g for g in gpus if g["vendor"] == vendor]
        if cand:
            best = cand[0]
            break
    hw["accel"] = (best or {}).get("api") or ("none" if not gpus else "")
    if any(g.get("unified") for g in gpus):
        hw["unified"] = True
    by_vendor: Dict[str, float] = {}
    for g in discrete:
        by_vendor[g["vendor"]] = by_vendor.get(g["vendor"], 0.0) + float(g["vram_gb"])
    hw["vram_gb"] = round(max(by_vendor.values()), 1) if by_vendor else 0.0
    return hw


def probe(refresh: bool = False, timeout: float = DEADLINE, use_cache: bool = True) -> Dict[str, Any]:
    """Everything we know about this machine. Cached per process; GPU facts cached on disk.

    Never takes much longer than `timeout` seconds. A result cut short by the deadline is
    marked partial and is not cached, so the next call tries again.
    """
    global _FULL
    with _LOCK:
        if _FULL is not None and not refresh:
            out = dict(_FULL)
            out["terminal"] = _terminal()
            return out
    t0 = time.time()
    hw = basics(refresh=refresh)
    gpus = _cache_load(hw) if use_cache and not refresh else None
    complete = True
    hw["gpu_cache"] = gpus is not None
    if gpus is None:
        gpus, complete = _slow_gpus(hw, max(0.05, timeout - (time.time() - t0)))
        if complete:
            _cache_save(hw, gpus)
    hw["gpus"] = gpus
    _finish(hw)
    hw["partial"] = not complete
    hw["probe_ms"] = round((time.time() - t0) * 1000, 1)
    if complete:
        with _LOCK:
            _FULL = dict(hw)
    return hw


def reset() -> None:
    """Forget the in-process cache (tests and `pulsarcode doctor --refresh`)."""
    global _BASICS, _FULL
    with _LOCK:
        _BASICS = None
        _FULL = None


# ---------------------------------------------------------------------------
# Advice
# ---------------------------------------------------------------------------

def gpu_label(g: Mapping[str, Any], with_vram: bool = True) -> str:
    name = short_gpu(str(g.get("name") or ""))
    if g.get("vendor") == "apple":
        cores = g.get("cores")
        return ("%d-core GPU" % cores) if cores else "GPU"
    v = _fmt_gb(g.get("vram_gb")) if with_vram else ""
    return (name + " " + v).strip()


def summary(hw: Optional[Mapping[str, Any]] = None, width: int = 60) -> str:
    """One line for the boot screen, e.g. 'Apple M3 Max · 16 cores · 64 GB · Metal'.

    Degrades to fit `width`: drops the environment tag, the compute API, VRAM, then cores.
    """
    hw = hw if hw is not None else probe()
    dot = " \u00b7 "
    chip = str(hw.get("chip") or "")
    cpu = chip or short_cpu(str(hw.get("cpu") or "")) or str(hw.get("arch") or "CPU")
    cores = hw.get("physical_cores") or hw.get("cores") or 0
    ram = hw.get("ram_limit_gb") or hw.get("ram_gb") or 0
    gpus = [g for g in (hw.get("gpus") or []) if not g.get("virtual")]
    compute = [g for g in gpus if g.get("vendor") != "apple"]
    env = hw.get("env") or {}
    tag = "WSL" if env.get("wsl") else (env.get("container") or "") if env.get("container") else \
        "Termux" if env.get("termux") else ""

    def gpu_part(with_vram: bool, with_api: bool) -> str:
        if chip and not compute:
            return "Metal" if with_api else ""
        if not compute:
            return "no GPU"
        g = compute[0]
        label = gpu_label(g, with_vram)
        if len(compute) > 1 and all(short_gpu(c["name"]) == short_gpu(g["name"]) for c in compute):
            label = "%dx %s" % (len(compute), label)
        elif len(compute) > 1:
            label += " +%d" % (len(compute) - 1)
        api = g.get("api") or ""
        if with_api and api and api != "Vulkan":
            label += dot + api
        return label

    variants = []
    for with_tag, with_api, with_vram, with_cores in ((1, 1, 1, 1), (0, 1, 1, 1), (0, 0, 1, 1), (0, 0, 0, 1),
                                                     (0, 0, 0, 0)):
        parts = [cpu]
        if with_cores and cores:
            parts.append("%d cores" % cores)
        if ram:
            parts.append(_fmt_gb(ram))
        gp = gpu_part(bool(with_vram), bool(with_api))
        if gp:
            parts.append(gp)
        if with_tag and tag:
            parts.append(tag)
        variants.append(dot.join(parts))
    for v in variants:
        if len(v) <= width:
            return v
    last = variants[-1]
    return last[: max(1, width - 1)] + "\u2026" if width > 1 else last[:width]


def fx_budget(hw: Optional[Mapping[str, Any]] = None, env: Optional[Mapping[str, str]] = None) -> str:
    """'full', 'lite' or 'minimal': how much animation this machine and link should carry.

    PULSAR_FX=full|lite|minimal forces a level (off/plain mean minimal). Otherwise: minimal for
    reduced motion and very small machines; lite over SSH, on 2-core or <4 GB devices,
    on the Linux console, in Termux and in the legacy Windows console; full everywhere else.
    """
    env = env if env is not None else os.environ
    mode = (env.get("PULSAR_FX") or "auto").strip().lower()
    if mode in ("full", "lite", "minimal"):
        return mode
    if mode in ("off", "0", "false", "none", "no", "plain") or env.get("PULSAR_REDUCED_MOTION") == "1":
        return "minimal"
    hw = hw if hw is not None else basics()
    cores = int(hw.get("cores") or 0)
    ram = float(hw.get("ram_limit_gb") or hw.get("ram_gb") or 0)
    if (cores and cores <= 1) or (ram and ram < 1.5):
        return "minimal"
    flags = hw.get("env") or {}
    term = (env.get("TERM") or "").lower()
    remote = bool(env.get("SSH_CONNECTION") or env.get("SSH_CLIENT") or env.get("SSH_TTY")) or bool(flags.get("ssh"))
    conhost = os.name == "nt" and not env.get("WT_SESSION") and not env.get("TERM_PROGRAM") and not env.get("TERM")
    if remote or (cores and cores <= 2) or (ram and ram < 4) or term == "linux" or flags.get("termux") or conhost:
        return "lite"
    return "full"


_HINTS: Tuple[Tuple[float, str], ...] = (
    (3, "cloud models recommended"),
    (6, "3-4B models, quick edits only"),
    (12, "7-9B coder models"),
    (20, "14B-class coders or gpt-oss-20b"),
    (32, "27-32B coders or 30B-A3B MoE"),
    (64, "30B-class coders at 8-bit, 70B at 4-bit"),
    (128, "120B-class MoE (gpt-oss-120b)"),
)


def local_model_hint(hw: Optional[Mapping[str, Any]] = None) -> str:
    """Short advice for Ollama-style local models, e.g. '64 GB unified: 30B-class coders at 8-bit, 70B at 4-bit'.

    Budget: 70% of unified memory, 90% of VRAM, or half of system RAM on CPU-only machines
    (4-bit weights plus room for a coding-agent context).
    """
    hw = hw if hw is not None else probe()
    ram = float(hw.get("ram_limit_gb") or hw.get("ram_gb") or 0)
    vram = float(hw.get("vram_gb") or 0)
    if hw.get("unified") and ram:
        mem, kind, budget = ram, "unified", ram * 0.70
    elif vram >= 4:
        mem, kind, budget = vram, "VRAM", vram * 0.90
    elif ram:
        mem, kind, budget = ram, "RAM, CPU only", ram * 0.50
    else:
        return "unknown hardware: start with a free cloud tier or a 7-9B model"
    text = "200B+ MoE models"
    for limit, label in _HINTS:
        if budget < limit:
            text = label
            break
    return "%d GB %s: %s" % (int(round(mem)), kind, text)
