"""Machine telemetry beside the GPU view: CPU, memory, disks, volumes, network adapters and hardware sensors.

The numbers here are reported by the OS (psutil, Windows performance counters, /proc and /sys) or by a sensor
source named in each reading (``sensors.py``). They are machine readings, never GPU bytes: nothing here is added
to ``Snapshot.attributed`` and ``diff`` ignores it. Unknown is ``None`` (``null`` in JSON), never 0; a rate needs two
readings, so the first reading of every rate is ``None``.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field

import psutil

from .sensors import SensorReading, SensorSource, open_sensors

VOLUME_REFRESH_S = 5.0  # partitions and their free space change slowly; disk_usage on a slow drive can take a while
ADDRESS_REFRESH_S = 5.0


@dataclass
class CpuInfo:
    model: str
    logical: int
    physical: int | None
    util_pct: float | None              # whole machine, 0-100; None on the first reading
    per_core_pct: list[float | None]
    per_core_mhz: list[float | None]    # effective clock per logical processor, as the OS reports it ([] when unknown)
    speed_mhz: float | None             # current effective clock as the OS reports it (Task Manager's "Speed")
    base_mhz: float | None
    max_mhz: float | None
    load_avg: tuple[float, float, float] | None  # 1/5/15 min; Windows has no load average
    processes: int | None
    threads: int | None
    handles: int | None                 # Windows only
    uptime_s: float | None
    speed_note: str = ""


@dataclass
class MemoryInfo:
    total: int
    used: int
    available: int
    cached: int | None        # page cache (Linux cached + buffers, macOS inactive); None where the OS doesn't say
    committed: int | None     # commit charge (Windows) / Committed_AS (Linux)
    commit_limit: int | None
    swap_total: int
    swap_used: int


@dataclass
class DiskIO:
    name: str
    model: str | None
    read_bps: float | None
    write_bps: float | None
    read_bytes: int
    write_bytes: int
    active_pct: float | None  # share of time the disk was busy (Task Manager's "Active time"); None where unknown


@dataclass
class Volume:
    mount: str
    device: str
    fstype: str
    total: int
    used: int
    free: int


@dataclass
class Nic:
    name: str
    up: bool
    speed_mbps: int | None    # link speed; None when the OS reports 0 (unknown, e.g. Wi-Fi on some drivers)
    mtu: int | None
    rx_bps: float | None
    tx_bps: float | None
    rx_bytes: int
    tx_bytes: int
    errors: int
    drops: int
    ipv4: list[str] = field(default_factory=list)


@dataclass
class HardwareSample:
    t: float
    cpu: CpuInfo
    memory: MemoryInfo
    disks: list[DiskIO]
    volumes: list[Volume]
    nics: list[Nic]
    sensors: SensorReading
    notes: list[str] = field(default_factory=list)
    cost_ms: float = 0.0


def cpu_model() -> str:
    """The marketing name of the CPU, read once: the registry on Windows, /proc/cpuinfo on Linux, sysctl on macOS."""
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        if sys.platform.startswith("linux"):
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                for line in f:
                    key, _, value = line.partition(":")
                    if key.strip() in ("model name", "Model", "Hardware") and value.strip():
                        return value.strip()
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=2)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return platform.processor() or platform.machine() or "CPU"


def _meminfo() -> dict[str, int]:
    """Linux /proc/meminfo in bytes."""
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="ascii", errors="replace") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    out[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    except OSError:
        pass
    return out


def _linux_threads() -> int | None:
    """Scheduling entities (threads) from /proc/loadavg's running/total field."""
    try:
        with open("/proc/loadavg", encoding="ascii") as f:
            total = f.read().split()[3].split("/")[1]
        return int(total)
    except (OSError, IndexError, ValueError):
        return None


def _linux_core_mhz(n: int) -> list[float | None]:
    """Each logical CPU's current clock from cpufreq (kHz), or [] when cpufreq is absent (VMs, containers)."""
    out: list[float | None] = []
    for i in range(n):
        try:
            with open(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq", encoding="ascii") as f:
                out.append(int(f.read().strip()) / 1000)
        except (OSError, ValueError):
            out.append(None)
    return out if any(v is not None for v in out) else []


def _disk_model(name: str) -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    for path in (f"/sys/block/{name}/device/model", f"/sys/block/{name}/device/device/model"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                model = f.read().strip()
            if model:
                return model
        except OSError:
            continue
    return None


def _win_core_mhz(win: dict, base: float | None, n: int) -> list[float | None]:
    """Per logical processor: base clock x its % Processor Performance (instances are "group,index")."""
    if not base:
        return []
    out: list[float | None] = [None] * n
    for name, perf in win.get("perf_each", []):
        group, _, index = name.partition(",")
        if group.isdigit() and index.isdigit():
            i = int(group) * 64 + int(index)
            if i < n:
                out[i] = base * perf / 100
    return out if any(v is not None for v in out) else []


def _physical_disks() -> set[str] | None:
    """Linux whole-disk block devices (no partitions, loop or RAM disks); None elsewhere (psutil already lists disks)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        return {d for d in os.listdir("/sys/block") if not d.startswith(("loop", "ram", "zram", "fd"))}
    except OSError:
        return None


class _WinCounters:
    """Task Manager's own counters through PDH: CPU speed, disk active time, commit charge, process/thread/handle counts."""

    PATHS = {
        "perf": r"\Processor Information(_Total)\% Processor Performance",
        "perf_each": r"\Processor Information(*)\% Processor Performance",
        "idle": r"\PhysicalDisk(*)\% Idle Time",
        "committed": r"\Memory\Committed Bytes",
        "limit": r"\Memory\Commit Limit",
        "cache": r"\Memory\Cache Bytes",
        "standby_core": r"\Memory\Standby Cache Core Bytes",
        "standby_normal": r"\Memory\Standby Cache Normal Priority Bytes",
        "standby_reserve": r"\Memory\Standby Cache Reserve Bytes",
        "modified": r"\Memory\Modified Page List Bytes",
        "processes": r"\System\Processes",
        "threads": r"\System\Threads",
        "handles": r"\Process(_Total)\Handle Count",
    }

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        from . import pdh

        self._ctypes, self._wintypes, self._pdh_mod = ctypes, wintypes, pdh
        self._lib = pdh._load_pdh()
        self._query = wintypes.HANDLE()
        if self._lib.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
            raise OSError("PdhOpenQuery failed")
        self._handles = {}
        for key, path in self.PATHS.items():
            h = wintypes.HANDLE()
            if self._lib.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(h)) == 0:
                self._handles[key] = h
        self._lib.PdhCollectQueryData(self._query)  # rate counters (% Idle Time, % Processor Performance) need a baseline

    def read(self) -> dict[str, list[tuple[str, float]]]:
        ctypes, wintypes, pdh = self._ctypes, self._wintypes, self._pdh_mod
        if self._lib.PdhCollectQueryData(self._query) != 0:
            return {}
        out: dict[str, list[tuple[str, float]]] = {}
        fmt = pdh.PDH_FMT_DOUBLE | pdh.PDH_FMT_NOCAP100
        for key, h in self._handles.items():
            size, count = wintypes.DWORD(0), wintypes.DWORD(0)
            if self._lib.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size), ctypes.byref(count), None) != pdh.PDH_MORE_DATA:
                continue
            buf = ctypes.create_string_buffer(size.value)
            if self._lib.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size), ctypes.byref(count), buf) != 0:
                continue
            items = ctypes.cast(buf, ctypes.POINTER(pdh._Item))
            out[key] = [(items[i].szName, items[i].FmtValue.u.doubleValue)
                        for i in range(count.value) if items[i].FmtValue.CStatus in (0, 1)]
        return out

    def close(self) -> None:
        if self._query:
            self._lib.PdhCloseQuery(self._query)
            self._query = self._wintypes.HANDLE()


def _first(values: dict[str, list[tuple[str, float]]], key: str) -> float | None:
    rows = values.get(key)
    return rows[0][1] if rows else None


def _rate(now: int, before: int | None, dt: float) -> float | None:
    """Bytes per second, or None for the first reading, a counter reset or no elapsed time."""
    if before is None or dt <= 0 or now < before:
        return None
    return (now - before) / dt


class HardwareSampler:
    """Samples machine telemetry. Every rate keeps its own previous reading, so this never disturbs
    ``psutil.cpu_percent``'s shared state that ``host.HostSampler`` relies on."""

    def __init__(self, sensors: bool = True, interval: float = 1.0, one_shot: bool = False) -> None:
        self._model = cpu_model()
        self._logical = psutil.cpu_count() or 1
        self._physical = psutil.cpu_count(logical=False)
        self._win: _WinCounters | None = None
        self.notes: list[str] = []
        if sys.platform == "win32":
            try:
                self._win = _WinCounters()
            except OSError as error:
                self.notes.append(f"Windows performance counters unavailable ({error}): CPU speed, disk active time "
                                  "and commit charge are not shown")
        self._sensors: SensorSource | None = open_sensors(interval=interval, one_shot=one_shot) if sensors else None
        self._disk_names = _physical_disks()
        self._last_t: float | None = None
        self._last_cpu = None
        self._last_disk: dict[str, tuple[int, int, int | None]] = {}
        self._models: dict[str, str | None] = {}
        self._last_net: dict[str, tuple[int, int]] = {}
        self._volumes: list[Volume] = []
        self._volumes_t = 0.0
        self._addrs: dict[str, list[str]] = {}
        self._addrs_t = 0.0

    def reset_stats(self) -> None:
        if self._sensors is not None:
            self._sensors.reset_stats()

    def close(self) -> None:
        if self._sensors is not None:
            self._sensors.close()
        if self._win is not None:
            self._win.close()

    def __enter__(self) -> HardwareSampler:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ parts

    def _cpu(self, now: float, win: dict) -> CpuInfo:
        times = psutil.cpu_times(percpu=True)
        per_core: list[float | None] = [None] * len(times)
        util = None
        if self._last_cpu is not None and len(self._last_cpu) == len(times):
            busy_total = all_total = 0.0
            for i, (a, b) in enumerate(zip(self._last_cpu, times)):
                total = sum(b) - sum(a)
                idle = (b.idle + getattr(b, "iowait", 0.0)) - (a.idle + getattr(a, "iowait", 0.0))
                if total > 0:
                    per_core[i] = min(100.0, max(0.0, 100.0 * (total - idle) / total))
                    busy_total += total - idle
                    all_total += total
            util = min(100.0, max(0.0, 100.0 * busy_total / all_total)) if all_total > 0 else None
        self._last_cpu = times
        base = max_mhz = speed = None
        note = ""
        try:
            freq = psutil.cpu_freq()
        except (OSError, NotImplementedError, AttributeError):
            freq = None
        if sys.platform == "win32":
            # psutil's "current" on Windows is the fixed base clock; Task Manager's speed is base x % Processor Performance.
            base = freq.max if freq and freq.max else None
            perf = _first(win, "perf")
            speed = base * perf / 100 if base and perf is not None else None
            if speed is None:
                note = "live CPU speed needs the Processor Information performance counter"
        elif freq:
            speed = freq.current or None
            max_mhz = freq.max or None
            base = None
        try:
            load = os.getloadavg() if hasattr(os, "getloadavg") and sys.platform != "win32" else None
        except OSError:
            load = None
        processes = threads = handles = None
        if win:
            p, t, h = _first(win, "processes"), _first(win, "threads"), _first(win, "handles")
            processes = int(p) if p is not None else None
            threads = int(t) if t is not None else None
            handles = int(h) if h is not None else None
        if processes is None:
            try:
                processes = len(psutil.pids())
            except OSError:
                processes = None
        if threads is None and sys.platform.startswith("linux"):
            threads = _linux_threads()
        try:
            uptime = max(0.0, time.time() - psutil.boot_time())
        except OSError:
            uptime = None
        if sys.platform == "win32":
            per_mhz = _win_core_mhz(win, base, len(times))
        elif sys.platform.startswith("linux"):
            per_mhz = _linux_core_mhz(len(times))
        else:
            per_mhz = []
        return CpuInfo(self._model, self._logical, self._physical, util, per_core, per_mhz, speed, base, max_mhz,
                       tuple(load) if load else None, processes, threads, handles, uptime, note)

    def _memory(self, win: dict) -> MemoryInfo:
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        cached = committed = limit = None
        if sys.platform.startswith("linux"):
            info = _meminfo()
            if "Cached" in info:
                cached = info["Cached"] + info.get("Buffers", 0) + info.get("SReclaimable", 0)
            committed, limit = info.get("Committed_AS"), info.get("CommitLimit")
        elif sys.platform == "win32" and win:
            c, lim = _first(win, "committed"), _first(win, "limit")
            committed = int(c) if c is not None else None
            limit = int(lim) if lim is not None else None
            # Task Manager's "Cached": standby lists plus modified pages.
            parts = [_first(win, k) for k in ("standby_core", "standby_normal", "standby_reserve", "modified")]
            if all(v is not None for v in parts):
                cached = int(sum(parts))
        elif sys.platform == "darwin":
            cached = getattr(vm, "inactive", None)
        return MemoryInfo(vm.total, vm.total - vm.available, vm.available, cached, committed, limit, sw.total, sw.used)

    def _disks(self, dt: float, win: dict) -> list[DiskIO]:
        try:
            counters = psutil.disk_io_counters(perdisk=True) or {}
        except (OSError, RuntimeError):
            counters = {}
        active: dict[str, float] = {}
        for name, idle in win.get("idle", []):
            index = name.split(" ", 1)[0]
            if index.isdigit():  # "0 C: D:" is PhysicalDrive0; "_Total" is skipped
                active[f"PhysicalDrive{index}"] = min(100.0, max(0.0, 100.0 - idle))
        out = []
        seen = {}
        for name, c in sorted(counters.items()):
            if self._disk_names is not None and name not in self._disk_names:
                continue
            busy_ms = getattr(c, "busy_time", None) if sys.platform.startswith("linux") else None
            before = self._last_disk.get(name)
            read_bps = _rate(c.read_bytes, before[0] if before else None, dt)
            write_bps = _rate(c.write_bytes, before[1] if before else None, dt)
            pct = active.get(name)
            if pct is None and busy_ms is not None and before and before[2] is not None and dt > 0 and busy_ms >= before[2]:
                pct = min(100.0, 100.0 * (busy_ms - before[2]) / (dt * 1000))
            seen[name] = (c.read_bytes, c.write_bytes, busy_ms)
            if name not in self._models:
                self._models[name] = _disk_model(name)
            out.append(DiskIO(name, self._models[name], read_bps, write_bps, c.read_bytes, c.write_bytes, pct))
        self._last_disk = seen
        return out

    def _volume_list(self, now: float) -> list[Volume]:
        if self._volumes and now - self._volumes_t < VOLUME_REFRESH_S:
            return self._volumes
        volumes, devices = [], set()
        try:
            parts = psutil.disk_partitions(all=False)
        except OSError:
            parts = []
        for p in parts:
            opts = p.opts or ""
            # Never touch optical or network drives: an empty CD drive errors and a network share can hang.
            if "cdrom" in opts or "remote" in opts or not p.fstype or p.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay"):
                continue
            if p.device in devices and not sys.platform == "win32":
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
            except (OSError, SystemError):
                continue
            devices.add(p.device)
            volumes.append(Volume(p.mountpoint, p.device, p.fstype, u.total, u.used, u.free))
        self._volumes, self._volumes_t = volumes, now
        return volumes

    def _nics(self, now: float, dt: float) -> list[Nic]:
        try:
            stats = psutil.net_if_stats()
            counters = psutil.net_io_counters(pernic=True) or {}
        except OSError:
            return []
        if now - self._addrs_t >= ADDRESS_REFRESH_S:
            self._addrs_t = now
            try:
                import socket

                self._addrs = {name: [a.address for a in addrs if a.family == socket.AF_INET]
                               for name, addrs in psutil.net_if_addrs().items()}
            except OSError:
                self._addrs = {}
        out, seen = [], {}
        for name, c in counters.items():
            s = stats.get(name)
            if name == "lo" or name.lower().startswith("loopback"):
                continue
            before = self._last_net.get(name)
            seen[name] = (c.bytes_recv, c.bytes_sent)
            out.append(Nic(name, bool(s and s.isup), (s.speed or None) if s else None, (s.mtu or None) if s else None,
                           _rate(c.bytes_recv, before[0] if before else None, dt),
                           _rate(c.bytes_sent, before[1] if before else None, dt),
                           c.bytes_recv, c.bytes_sent, c.errin + c.errout, c.dropin + c.dropout,
                           self._addrs.get(name, [])))
        self._last_net = seen
        out.sort(key=lambda n: (not n.up, -(n.rx_bytes + n.tx_bytes)))
        return out

    # ------------------------------------------------------------------ sample

    def sample(self) -> HardwareSample:
        t0 = time.perf_counter()
        now = time.monotonic()
        dt = now - self._last_t if self._last_t is not None else 0.0
        self._last_t = now
        win = self._win.read() if self._win is not None else {}
        cpu = self._cpu(now, win)
        memory = self._memory(win)
        disks = self._disks(dt, win)
        volumes = self._volume_list(now)
        nics = self._nics(now, dt)
        sensors = self._sensors.read() if self._sensors is not None else SensorReading([], ["sensors off (--no-sensors)"])
        return HardwareSample(time.time(), cpu, memory, disks, volumes, nics, sensors, list(self.notes),
                              (time.perf_counter() - t0) * 1000)


def to_dict(h: HardwareSample) -> dict:
    """The --json "hardware" section. Unknown stays null."""
    cpu = asdict(h.cpu)
    cpu["load_avg"] = [round(v, 2) for v in h.cpu.load_avg] if h.cpu.load_avg else None
    cpu["speed_note"] = h.cpu.speed_note or None
    for key in ("util_pct", "speed_mhz", "base_mhz", "max_mhz"):
        if cpu[key] is not None:
            cpu[key] = round(cpu[key], 1)
    cpu["per_core_pct"] = [round(v, 1) if v is not None else None for v in h.cpu.per_core_pct]
    cpu["per_core_mhz"] = [round(v) if v is not None else None for v in h.cpu.per_core_mhz]
    if cpu["uptime_s"] is not None:
        cpu["uptime_s"] = int(cpu["uptime_s"])

    def rounded(d: dict) -> dict:
        return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in d.items()}

    return {
        "cpu": cpu,
        "memory": asdict(h.memory),
        "disks": [rounded(asdict(d)) for d in h.disks],
        "volumes": [asdict(v) for v in h.volumes],
        "network": [rounded(asdict(n)) for n in h.nics],
        "sensors": [s.to_dict() for s in h.sensors.sensors],
        "drive_health": [d.to_dict() for d in h.sensors.health],
        "sensor_source": h.sensors.source or None,
        "notes": h.notes + h.sensors.notes,
        "cost_ms": round(h.cost_ms, 1),
    }


def _rate_text(bps: float | None) -> str:
    if bps is None:
        return "…"
    for unit, scale in (("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3)):
        if bps >= scale:
            return f"{bps / scale:.1f} {unit}"
    return f"{bps:.0f} B/s"


def _gb(n: int | float | None) -> str:
    return "n/a" if n is None else f"{n / 2**30:.1f} GB"


def to_text(h: HardwareSample) -> str:
    """The --once "Hardware" section."""
    c, m = h.cpu, h.memory
    lines = ["Hardware (OS- and sensor-reported; never GPU bytes)"]
    speed = f"{c.speed_mhz / 1000:.2f} GHz" if c.speed_mhz else "n/a"
    util = f"{c.util_pct:.0f}%" if c.util_pct is not None else "…"
    lines.append(f"  CPU      {c.model}: {util}, {speed}, {c.logical} threads"
                 + (f"/{c.physical} cores" if c.physical else "")
                 + (f", {c.processes} processes" if c.processes is not None else "")
                 + (f", {c.threads} threads" if c.threads is not None else ""))
    lines.append(f"  Memory   {_gb(m.used)} / {_gb(m.total)} used, {_gb(m.available)} available"
                 + (f", cached {_gb(m.cached)}" if m.cached is not None else "")
                 + (f", committed {_gb(m.committed)} / {_gb(m.commit_limit)}" if m.committed is not None else ""))
    for d in h.disks:
        active = f", active {d.active_pct:.0f}%" if d.active_pct is not None else ""
        lines.append(f"  Disk     {d.name}: read {_rate_text(d.read_bps)}, write {_rate_text(d.write_bps)}{active}")
    for v in h.volumes:
        lines.append(f"  Volume   {v.mount} ({v.fstype}): {_gb(v.used)} / {_gb(v.total)} used, {_gb(v.free)} free")
    for n in h.nics:
        if not n.up:
            continue
        link = f", link {n.speed_mbps} Mb/s" if n.speed_mbps else ""
        addr = f", {', '.join(n.ipv4)}" if n.ipv4 else ""
        lines.append(f"  Network  {n.name}: down {_rate_text(n.rx_bps)}, up {_rate_text(n.tx_bps)}{link}{addr}")
    if h.sensors.sensors:
        lines.append(f"  Sensors  ({h.sensors.source})")
        for s in h.sensors.sensors:
            if s.kind in ("temperature", "fan", "power", "voltage", "clock") and s.value is not None:
                lines.append(f"    {s.hardware} / {s.name}: {s.text()}")
    for note in h.notes + h.sensors.notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)
