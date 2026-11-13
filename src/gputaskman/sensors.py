"""Hardware sensors: temperatures, fans, power, voltages, clocks, and drive health (S.M.A.R.T.).

Every reading names its source:

* ``lhm``: Windows, LibreHardwareMonitor through the bundled ``gputaskman-sensors.exe`` helper. CPU, motherboard
  and memory sensors need the PawnIO driver (installed by the Windows installer, never shipped in the app folder)
  and an elevated gputaskman; without them only the sensors LibreHardwareMonitor reaches without a driver remain.
  Drive health comes from the helper too (Windows Storage Management API, ``wmi``).
* ``hwmon``: Linux ``/sys/class/hwmon``, the kernel drivers' own readings, with their limits (``tempN_crit``:
  TjMax on Intel's coretemp).
* ``rapl``: Linux ``/sys/class/powercap`` energy counters (power is the rate between two readings; ``None`` on the
  first; root-only on current kernels) and power limits (PL1/PL2, readable by everyone).
* ``smartctl``: Linux drive health from smartmontools, when it is installed and gputaskman runs as root.
* ``battery``: psutil.

A sensor that exists but cannot be read is ``None``, never 0; NaN and infinities are ``None`` too. Min/avg/max are
this session's (since start or the last reset). These are sensor readings, never GPU bytes, and nothing here is
summed into a GPU total. Coverage gaps are explained in ``SensorReading.notes``.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

# LibreHardwareMonitor SensorType -> (kind, unit)
LHM_TYPES = {
    "Voltage": ("voltage", "V"), "Current": ("current", "A"), "Power": ("power", "W"), "Clock": ("clock", "MHz"),
    "Temperature": ("temperature", "°C"), "Load": ("load", "%"), "Frequency": ("frequency", "Hz"), "Fan": ("fan", "RPM"),
    "Flow": ("flow", "L/h"), "Control": ("control", "%"), "Level": ("level", "%"), "Factor": ("factor", ""),
    "Data": ("data", "GB"), "SmallData": ("data", "MB"), "Throughput": ("throughput", "B/s"), "TimeSpan": ("time", "s"),
    "Energy": ("energy", "mWh"), "Noise": ("noise", "dBA"), "Conductivity": ("conductivity", "µS/cm"),
    "Humidity": ("humidity", "%"),
}
KIND_ORDER = ("temperature", "power", "clock", "voltage", "current", "fan", "control", "load", "level", "frequency",
              "flow", "throughput", "data", "energy", "factor", "time", "noise", "humidity", "conductivity")
KIND_TITLES = {"temperature": "Temperatures", "power": "Power", "clock": "Clocks", "voltage": "Voltages",
               "current": "Currents", "fan": "Fans", "control": "Fan and pump control", "load": "Load",
               "level": "Levels", "frequency": "Frequencies", "flow": "Flow", "throughput": "Throughput",
               "data": "Data", "energy": "Energy", "factor": "Factors", "time": "Time", "noise": "Noise",
               "humidity": "Humidity", "conductivity": "Conductivity"}
HELPER_NAME = "gputaskman-sensors.exe"
HELPER_TIMEOUT_S = 20.0  # LibreHardwareMonitor's first open enumerates SMBus, storage and GPUs; it can take seconds
SMART_INTERVAL_S = 300.0
SMART_TIMEOUT_S = 10.0

# Which part of the machine a sensor belongs to, for the hardware view's hierarchy.
LHM_COMPONENT = {"Cpu": "cpu", "Memory": "memory", "GpuNvidia": "gpu", "GpuAmd": "gpu", "GpuIntel": "gpu",
                 "Storage": "storage", "Motherboard": "board", "SuperIO": "board", "EmbeddedController": "board",
                 "Psu": "board", "Cooler": "board", "Controller": "board", "Battery": "battery", "Network": "network"}
HWMON_CPU = ("k10temp", "coretemp", "zenpower", "cpu_thermal", "cpu-thermal", "via_cputemp", "fam15h_power")
HWMON_GPU = ("amdgpu", "radeon", "nouveau", "i915", "xe")
HWMON_STORAGE = ("nvme", "drivetemp")
HWMON_MEMORY = ("jc42", "spd5118", "ee1004")
HWMON_NETWORK = ("iwlwifi", "ath", "mt76", "mt79", "r8169", "igc", "e1000", "rtw", "bnxt", "mlx")


def finite(v) -> float | None:
    """A usable number, or None: unknown, NaN and infinities are not readings."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


@dataclass
class Sensor:
    key: str
    hardware: str       # the chip or device: "AMD Ryzen 9 7950X", "nct6799", "Samsung SSD 990 PRO"
    group: str          # what kind of hardware: Cpu, GpuNvidia, Motherboard, Storage, Battery, or the hwmon driver name
    name: str
    kind: str           # temperature, fan, power, voltage, current, clock, load, ...
    value: float | None
    unit: str
    source: str         # lhm, hwmon, rapl, battery
    min: float | None = None
    max: float | None = None
    avg: float | None = None
    limit: float | None = None  # the device's own critical limit (hwmon tempN_crit: TjMax on coretemp)

    def text(self, value: float | None = None) -> str:
        return fmt(self.kind, self.unit, self.value if value is None else value)

    def to_dict(self) -> dict:
        def r(x: float | None) -> float | None:
            return round(x, 3) if x is not None else None

        return {"hardware": self.hardware, "group": self.group, "name": self.name, "kind": self.kind,
                "value": r(self.value), "unit": self.unit, "min": r(self.min), "avg": r(self.avg), "max": r(self.max),
                "limit": r(self.limit), "source": self.source}


def fmt(kind: str, unit: str, v: float | None) -> str:
    v = finite(v)
    if v is None:
        return "n/a"
    if unit == "B/s":
        for name, scale in (("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3)):
            if abs(v) >= scale:
                return f"{v / scale:.1f} {name}"
        return f"{v:.0f} B/s"
    if kind in ("fan", "clock") or unit in ("RPM", "MHz"):
        return f"{v:.0f} {unit}"
    if kind == "voltage":
        return f"{v:.3f} {unit}"
    if unit == "%":
        return f"{v:.1f}%"
    if unit == "s" and kind == "time":
        return f"{v / 3600:.1f} h" if v >= 3600 else f"{v:.0f} s"
    if unit:
        return f"{v:.1f} {unit}"
    return f"{v:.2f}"


@dataclass
class DiskHealth:
    """One physical drive's health as the OS or smartmontools reports it. Unknown fields are None."""
    index: str | None          # Windows physical disk number ("0" = PhysicalDrive0); Linux device name ("nvme0", "sda")
    model: str
    serial: str | None = None
    media: str | None = None   # SSD, HDD, SCM
    bus: str | None = None     # NVMe, SATA, USB, ...
    size: int | None = None
    health: str | None = None  # healthy, warning, failing (None: unknown)
    status: str | None = None  # the source's own words (OperationalStatus, smart_status)
    temperature: float | None = None
    temperature_max: float | None = None
    power_on_hours: float | None = None
    wear_pct: float | None = None  # life used (NVMe percentage used, SSD wear)
    read_errors: int | None = None
    write_errors: int | None = None
    reallocated: int | None = None
    pending: int | None = None
    media_errors: int | None = None
    source: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class SensorReading:
    sensors: list[Sensor]
    notes: list[str] = field(default_factory=list)  # why coverage is partial: no driver, not elevated, root-only
    source: str = ""
    health: list[DiskHealth] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[Sensor]:
        return [s for s in self.sensors if s.kind == kind]


class SensorSource:
    """Base: a source of SensorReadings. Min/avg/max are tracked here, per sensor, since start or the last reset."""

    name = ""

    def __init__(self) -> None:
        self._stats: dict[str, list[float]] = {}  # key -> [min, max, total, count]
        self._reset = False
        self._stats_lock = threading.Lock()

    def reset_stats(self) -> None:
        """Called from the UI thread; the next read starts fresh."""
        self._reset = True

    def track(self, sensors: list[Sensor]) -> list[Sensor]:
        with self._stats_lock:
            if self._reset:
                self._stats, self._reset = {}, False
            for s in sensors:
                s.value = finite(s.value)
                if s.value is None:
                    st = self._stats.get(s.key)
                    s.min, s.max, s.avg = (st[0], st[1], st[2] / st[3]) if st else (None, None, None)
                    continue
                st = self._stats.get(s.key)
                if st is None:
                    st = self._stats[s.key] = [s.value, s.value, 0.0, 0]
                st[0], st[1] = min(st[0], s.value), max(st[1], s.value)
                st[2] += s.value
                st[3] += 1
                s.min, s.max, s.avg = st[0], st[1], st[2] / st[3]
        return sensors

    def read(self) -> SensorReading:
        raise NotImplementedError

    def close(self) -> None:
        pass


def unique_keys(sensors: list[Sensor]) -> list[Sensor]:
    """No two sensors share a key, even when a source repeats one (two identical drives, a duplicated id)."""
    seen: dict[str, int] = {}
    for s in sensors:
        n = seen.get(s.key, 0)
        seen[s.key] = n + 1
        if n:
            s.key = f"{s.key}#{n}"
    return sensors


def _sort(sensors: list[Sensor]) -> list[Sensor]:
    order = {k: i for i, k in enumerate(KIND_ORDER)}
    return sorted(sensors, key=lambda s: (s.group != "Cpu", s.hardware.lower(), order.get(s.kind, 99), s.name.lower()))


def component_of(s: Sensor) -> str:
    """cpu, memory, gpu, storage, network, board or battery: where the hardware view lists this sensor."""
    if s.source == "lhm":
        return LHM_COMPONENT.get(s.group, "board")
    if s.source == "rapl" or s.group == "Cpu":
        return "cpu"
    if s.group == "Battery" or s.source == "battery":
        return "battery"
    g = s.group.lower()
    for names, comp in ((HWMON_CPU, "cpu"), (HWMON_GPU, "gpu"), (HWMON_STORAGE, "storage"), (HWMON_MEMORY, "memory"),
                        (HWMON_NETWORK, "network")):
        if any(g == n or g.startswith(n) for n in names):
            return comp
    return "board"


# ---------------------------------------------------------------------------------------------- battery (all OSes)

def battery_sensors() -> list[Sensor]:
    try:
        b = psutil.sensors_battery()
    except (OSError, NotImplementedError, AttributeError, RuntimeError):
        return []
    if b is None:
        return []
    out = [Sensor("battery/charge", "Battery", "Battery", "Charge", "level", finite(b.percent), "%", "battery")]
    if b.power_plugged is not None:
        out.append(Sensor("battery/plugged", "Battery", "Battery", "On AC power", "factor",
                          1.0 if b.power_plugged else 0.0, "", "battery"))
    if isinstance(b.secsleft, (int, float)) and b.secsleft >= 0 and not b.power_plugged:
        out.append(Sensor("battery/remaining", "Battery", "Battery", "Time remaining", "time", float(b.secsleft), "s", "battery"))
    return out


# ---------------------------------------------------------------------------------------------- Windows: LHM helper

def helper_path() -> Path | None:
    """The sensor helper: GPUTASKMAN_SENSORS_HELPER, else sensors/ beside a frozen gputaskman.exe."""
    env = os.environ.get("GPUTASKMAN_SENSORS_HELPER")
    if env:
        return Path(env) if Path(env).is_file() else None
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).resolve().parent / "sensors" / HELPER_NAME
        return candidate if candidate.is_file() else None
    return None


WIN_HEALTH = {0: "healthy", 1: "warning", 2: "failing"}
WIN_MEDIA = {3: "HDD", 4: "SSD", 5: "SCM"}
WIN_BUS = {1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA", 6: "Fibre Channel", 7: "USB", 8: "RAID", 9: "iSCSI",
           10: "SAS", 11: "SATA", 12: "SD", 13: "MMC", 15: "File-backed", 16: "Storage Spaces", 17: "NVMe"}


def _int(v) -> int | None:
    v = finite(v)
    return int(v) if v is not None else None


def parse_windows_disks(rows) -> list[DiskHealth]:
    """The helper's "disks" list (MSFT_PhysicalDisk + MSFT_StorageReliabilityCounter)."""
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        hours = finite(row.get("power_on_hours"))
        out.append(DiskHealth(
            index=str(row["device_id"]) if row.get("device_id") is not None else None,
            model=str(row.get("model") or "Drive").strip(), serial=(str(row["serial"]).strip() or None) if row.get("serial") else None,
            media=WIN_MEDIA.get(_int(row.get("media_type"))), bus=WIN_BUS.get(_int(row.get("bus_type"))),
            size=_int(row.get("size")), health=WIN_HEALTH.get(_int(row.get("health_status"))),
            status=str(row["operational_status"]) if row.get("operational_status") else None,
            temperature=finite(row.get("temperature")), temperature_max=finite(row.get("temperature_max")),
            power_on_hours=hours, wear_pct=finite(row.get("wear")),
            read_errors=_int(row.get("read_errors")), write_errors=_int(row.get("write_errors")),
            source="wmi"))
    return out


def parse_lhm(line: str | dict) -> tuple[list[Sensor], dict]:
    """One helper JSON line -> sensors plus its status fields (admin, pawnio, error, disks).

    Tolerant: an entry of the wrong shape is skipped, never raised, so one odd device cannot blank the view."""
    data = json.loads(line) if isinstance(line, str) else line
    if not isinstance(data, dict):
        raise ValueError("helper line is not an object")
    listed = data.get("hardware")
    hardware = [hw for hw in listed if isinstance(hw, dict)] if isinstance(listed, list) else []
    names = {str(hw.get("id")): str(hw.get("name") or hw.get("id")) for hw in hardware}
    sensors = []
    for hw in hardware:
        label = str(hw.get("name") or hw.get("id") or "?")
        parent = hw.get("parent")
        if parent is not None and str(parent) in names and names[str(parent)] != label:
            label = f"{names[str(parent)]} / {label}"  # e.g. "ROG STRIX X670E-E / Nuvoton NCT6799D"
        readings = hw.get("sensors")
        for s in readings if isinstance(readings, list) else []:
            if not isinstance(s, dict):
                continue
            kind, unit = LHM_TYPES.get(str(s.get("type", "")), (str(s.get("type") or "other").lower(), ""))
            sensors.append(Sensor(
                key=str(s.get("id") or f"{hw.get('id')}/{s.get('name')}"), hardware=label, group=str(hw.get("type") or ""),
                name=str(s.get("name") or "?"), kind=kind, value=finite(s.get("value")), unit=unit, source="lhm"))
    status = {"admin": bool(data.get("admin")), "pawnio": bool(data.get("pawnio")), "error": data.get("error"),
              "t": data.get("t"), "disks": parse_windows_disks(data.get("disks")), "disk_error": data.get("disk_error")}
    return unique_keys(sensors), status


def lhm_notes(status: dict, sensors: list[Sensor]) -> list[str]:
    notes = []
    if status.get("error"):
        notes.append(f"LibreHardwareMonitor: {status['error']}")
    if not status.get("pawnio"):
        notes.append("PawnIO driver not installed: CPU temperatures, package power, fans and voltages need it "
                     "(reinstall gputaskman with the sensor-driver option, or install PawnIO from pawnio.eu)")
    if not status.get("admin"):
        notes.append("not elevated: run gputaskman as administrator (Start menu: GPU Task Manager (administrator)) "
                     "for CPU, motherboard and memory sensors and drive reliability counters")
    if status.get("disk_error"):
        notes.append(f"drive health: {status['disk_error']}")
    if not any(s.group == "Cpu" and s.kind == "temperature" and s.value is not None for s in sensors) and not notes:
        notes.append("LibreHardwareMonitor reports no CPU temperature for this processor")
    return notes


class LhmHelper(SensorSource):
    """Runs the helper and keeps its newest line. The helper exits when its stdin closes, so it never outlives us."""

    name = "LibreHardwareMonitor (helper)"

    def __init__(self, exe: Path, interval: float = 1.0) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._latest: tuple[list[Sensor], dict] | None = None
        self._error: str | None = None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [str(exe), "--interval", str(max(250, int(interval * 1000)))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=flags, cwd=str(exe.parent))
        self._thread = threading.Thread(target=self._pump, name="gputaskman-sensors", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        try:
            for raw in self._proc.stdout:
                try:
                    parsed = parse_lhm(raw.decode("utf-8", errors="replace"))
                except Exception as error:  # noqa: BLE001 - a bad line must never stop the reader
                    with self._lock:
                        self._error = f"unreadable helper output ({type(error).__name__})"
                    continue
                with self._lock:
                    self._latest, self._error = parsed, None
        except (OSError, ValueError):
            pass
        code = self._proc.wait()
        with self._lock:
            if self._latest is None or code:
                self._error = f"sensor helper exited (code {code})"

    def read(self) -> SensorReading:
        with self._lock:
            latest, error = self._latest, self._error
        extra = battery_sensors()
        if latest is None:
            notes = [error] if error else ["LibreHardwareMonitor is starting (first reading takes a few seconds)"]
            return SensorReading(self.track(extra), notes, self.name)
        found, status = latest
        found = [Sensor(**s.__dict__) for s in found]  # our copies: tracking writes min/avg/max into them
        notes = lhm_notes(status, found) + ([error] if error else [])
        if not any(s.group == "Battery" for s in found):
            found += extra
        return SensorReading(_sort(self.track(unique_keys(found))), notes, self.name, list(status.get("disks") or []))

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self._proc.kill()


class LhmOnce(SensorSource):
    """One-shot (--once/--json): run the helper with --once and keep that single reading."""

    name = "LibreHardwareMonitor (helper)"

    def __init__(self, exe: Path) -> None:
        super().__init__()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            out = subprocess.run([str(exe), "--once"], capture_output=True, timeout=HELPER_TIMEOUT_S,
                                 creationflags=flags, cwd=str(exe.parent), stdin=subprocess.DEVNULL)
            lines = [line for line in out.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
            self._result = parse_lhm(lines[-1]) if lines else None
            self._error = None if lines else f"sensor helper printed nothing (code {out.returncode})"
        except subprocess.TimeoutExpired:
            self._result, self._error = None, f"sensor helper timed out after {HELPER_TIMEOUT_S:.0f} s"
        except Exception as error:  # noqa: BLE001 - the one-shot report must not fail for a sensor problem
            self._result, self._error = None, f"sensor helper failed: {type(error).__name__}: {error}"

    def read(self) -> SensorReading:
        if self._result is None:
            return SensorReading(self.track(battery_sensors()), [self._error or "no sensor reading"], self.name)
        found, status = self._result
        if not any(s.group == "Battery" for s in found):
            found = found + battery_sensors()
        return SensorReading(_sort(self.track(unique_keys(found))), lhm_notes(status, found), self.name,
                             list(status.get("disks") or []))


# ---------------------------------------------------------------------------------------------- Linux: hwmon + RAPL

_HWMON_FILE = re.compile(r"^(temp|fan|in|curr|power|freq)(\d+)_(input|average)$")
_HWMON_KIND = {"temp": ("temperature", "°C", 1e-3), "fan": ("fan", "RPM", 1.0), "in": ("voltage", "V", 1e-3),
               "curr": ("current", "A", 1e-3), "power": ("power", "W", 1e-6), "freq": ("clock", "MHz", 1e-6)}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except (OSError, ValueError):
        return None


def _num(path: Path, scale: float = 1.0) -> float | None:
    raw = _read(path)
    try:
        return int(raw) * scale if raw is not None else None
    except ValueError:
        return None


def _hwmon_device(d: Path, chip: str) -> str:
    """A readable device name: the NVMe or drive model, else the driver name."""
    model = _read(d / "device" / "model")
    return f"{chip} ({model})" if model else chip


def _smart_device(name: str) -> str:
    """/dev/nvme0 -> nvme0; /dev/sda -> sda."""
    return name.rsplit("/", 1)[-1]


def parse_smartctl(data: dict) -> DiskHealth:
    """``smartctl -j -i -H -A`` output -> DiskHealth. Missing fields stay None."""
    dev = data.get("device") or {}
    passed = (data.get("smart_status") or {}).get("passed")
    nvme = data.get("nvme_smart_health_information_log") or {}
    attrs = {a.get("id"): a for a in (data.get("ata_smart_attributes") or {}).get("table") or [] if isinstance(a, dict)}

    def raw(attr_id: int) -> int | None:
        a = attrs.get(attr_id)
        return _int((a.get("raw") or {}).get("value")) if a else None

    wear = finite(nvme.get("percentage_used"))
    if wear is None and 231 in attrs:  # SSD life left (normalized) on many SATA SSDs
        left = finite(attrs[231].get("value"))
        wear = 100 - left if left is not None else None
    health = None if passed is None else ("healthy" if passed else "failing")
    if health == "healthy" and (nvme.get("critical_warning") or 0):
        health = "warning"
    rotation = data.get("rotation_rate")
    return DiskHealth(
        index=_smart_device(str(dev.get("name") or "")) or None,
        model=str(data.get("model_name") or data.get("model_family") or dev.get("name") or "Drive"),
        serial=data.get("serial_number"), media="HDD" if isinstance(rotation, int) and rotation > 0 else (
            "SSD" if rotation == 0 or nvme else None),
        bus=str(dev.get("protocol") or dev.get("type") or "") or None,
        size=_int((data.get("user_capacity") or {}).get("bytes")), health=health,
        status="PASSED" if passed else ("FAILED" if passed is False else None),
        temperature=finite((data.get("temperature") or {}).get("current")),
        power_on_hours=finite((data.get("power_on_time") or {}).get("hours")), wear_pct=wear,
        reallocated=raw(5), pending=raw(197), media_errors=_int(nvme.get("media_errors")), source="smartctl")


class SmartPoller:
    """Linux drive health from smartmontools, polled in the background (smartctl can take seconds per drive)."""

    def __init__(self) -> None:
        self.health: list[DiskHealth] = []
        self.note: str | None = None
        self._stop = threading.Event()
        exe = shutil.which("smartctl")
        if exe is None:
            self.note = "drive health (S.M.A.R.T.) needs smartmontools (smartctl) installed"
            return
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            self.note = "drive health (S.M.A.R.T.) needs root: run gputaskman with sudo"
            return
        self._exe = exe
        threading.Thread(target=self._loop, name="gputaskman-smart", daemon=True).start()

    def _run(self, *args: str) -> dict:
        out = subprocess.run([self._exe, "-j", *args], capture_output=True, timeout=SMART_TIMEOUT_S, stdin=subprocess.DEVNULL)
        return json.loads(out.stdout.decode("utf-8", errors="replace") or "{}")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                devices = [d.get("name") for d in self._run("--scan").get("devices") or [] if isinstance(d, dict)]
                found = []
                for name in devices[:32]:
                    if name:
                        try:
                            found.append(parse_smartctl(self._run("-i", "-H", "-A", str(name))))
                        except (OSError, ValueError, subprocess.TimeoutExpired):
                            continue
                self.health, self.note = found, None
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                self.note = f"smartctl failed: {type(error).__name__}"
            self._stop.wait(SMART_INTERVAL_S)

    def close(self) -> None:
        self._stop.set()


class LinuxSensors(SensorSource):
    name = "hwmon + RAPL"

    def __init__(self, hwmon_root: Path = Path("/sys/class/hwmon"), rapl_root: Path = Path("/sys/class/powercap"),
                 supply_root: Path = Path("/sys/class/power_supply"), smart: bool = True) -> None:
        super().__init__()
        self._hwmon_root, self._rapl_root, self._supply_root = hwmon_root, rapl_root, supply_root
        self._rapl_last: dict[str, tuple[int, float]] = {}
        self._rapl_denied = False
        self._smart = SmartPoller() if smart else None

    def _hwmon(self) -> list[Sensor]:
        out = []
        try:
            chips = sorted(self._hwmon_root.iterdir(), key=lambda p: p.name)
        except OSError:
            return out
        for d in chips:
            chip = _read(d / "name") or d.name
            device = _hwmon_device(d, chip)
            try:
                files = sorted(f.name for f in d.iterdir())
            except OSError:
                continue
            seen: set[tuple[str, str]] = set()
            for fname in files:
                m = _HWMON_FILE.match(fname)
                if not m:
                    continue
                prefix, num, suffix = m.groups()
                if (prefix, num) in seen:  # power1_input and power1_average are the same sensor; prefer input
                    continue
                if suffix == "average" and f"{prefix}{num}_input" in files:
                    continue
                seen.add((prefix, num))
                kind, unit, scale = _HWMON_KIND[prefix]
                label = _read(d / f"{prefix}{num}_label") or f"{prefix}{num}"
                limit = None
                if prefix == "temp":  # the chip's critical limit; on coretemp it is TjMax
                    limit = _num(d / f"temp{num}_crit", scale)
                out.append(Sensor(f"hwmon/{d.name}/{prefix}{num}", device, chip, label, kind, _num(d / fname, scale),
                                  unit, "hwmon", limit=limit))
        return out

    def _rapl(self) -> list[Sensor]:
        out = []
        now = time.monotonic()
        try:
            zones = sorted(p for p in self._rapl_root.iterdir() if p.name.startswith(("intel-rapl:", "amd-rapl:")))
        except OSError:
            return out
        for z in zones:
            name = _read(z / "name") or z.name
            label = {"package-0": "Package", "package-1": "Package 1", "core": "Cores", "uncore": "Uncore/iGPU",
                     "dram": "DRAM", "psys": "Platform"}.get(name, name)
            if name.startswith("package"):  # power limits (PL1/PL2): readable without root
                for c in range(3):
                    limit = _num(z / f"constraint_{c}_power_limit_uw", 1e-6)
                    if limit is None:
                        continue
                    which = _read(z / f"constraint_{c}_name") or f"constraint {c}"
                    short = {"long_term": "PL1 (long term, TDP)", "short_term": "PL2 (short term)",
                             "peak_power": "PL4 (peak)"}.get(which, which)
                    out.append(Sensor(f"rapl/{z.name}/limit{c}", "CPU (RAPL)", "Cpu", f"{label} power limit {short}",
                                      "power", limit, "W", "rapl"))
            try:
                energy = int((z / "energy_uj").read_text())
            except PermissionError:
                self._rapl_denied = True
                continue
            except (OSError, ValueError):
                continue
            value = None
            before = self._rapl_last.get(z.name)
            if before is not None and now > before[1]:
                delta = energy - before[0]
                if delta < 0:  # the counter wrapped
                    rng = _num(z / "max_energy_range_uj")
                    delta = delta + int(rng) if rng else -1
                if delta >= 0:
                    value = delta / 1e6 / (now - before[1])
            self._rapl_last[z.name] = (energy, now)
            out.append(Sensor(f"rapl/{z.name}", "CPU (RAPL)", "Cpu", f"{label} power", "power", value, "W", "rapl"))
        return out

    def _battery_power(self) -> list[Sensor]:
        out = []
        try:
            supplies = sorted(self._supply_root.iterdir())
        except OSError:
            return out
        for s in supplies:
            if _read(s / "type") != "Battery":
                continue
            watts = _num(s / "power_now", 1e-6)
            if watts is None:
                cur, volt = _num(s / "current_now"), _num(s / "voltage_now")
                watts = cur * volt / 1e12 if cur is not None and volt is not None else None
            status = _read(s / "status") or ""
            out.append(Sensor(f"battery/{s.name}/power", "Battery", "Battery",
                              f"{s.name} {'discharge' if status == 'Discharging' else 'charge'} rate", "power",
                              watts, "W", "battery"))
            wear_full, wear_design = _num(s / "energy_full"), _num(s / "energy_full_design")
            if wear_full and wear_design:
                out.append(Sensor(f"battery/{s.name}/capacity", "Battery", "Battery", f"{s.name} capacity vs design",
                                  "level", 100 * wear_full / wear_design, "%", "battery"))
        return out

    def read(self) -> SensorReading:
        sensors = self._hwmon() + self._rapl() + battery_sensors() + self._battery_power()
        notes = []
        if self._rapl_denied:
            notes.append("CPU package power (RAPL) needs root: the energy counters are root-only on current kernels")
        if not any(s.source == "hwmon" for s in sensors):
            notes.append("no hwmon sensors found (containers and VMs usually expose none)")
        health = []
        if self._smart is not None:
            health = list(self._smart.health)
            if self._smart.note:
                notes.append(self._smart.note)
        return SensorReading(_sort(self.track(unique_keys(sensors))), notes, self.name, health)

    def close(self) -> None:
        if self._smart is not None:
            self._smart.close()


class NoSensors(SensorSource):
    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason
        self.name = "none"

    def read(self) -> SensorReading:
        battery = battery_sensors()
        return SensorReading(self.track(battery), [self.reason], "battery only" if battery else "")


def open_sensors(interval: float = 1.0, one_shot: bool = False) -> SensorSource:
    """The best sensor source on this machine, or one that explains what is missing."""
    if sys.platform == "win32":
        exe = helper_path()
        if exe is None:
            return NoSensors("temperatures, fans, power and drive health need the sensor helper, which ships with the "
                             "installer and portable builds (not with pip/uv installs)")
        try:
            return LhmOnce(exe) if one_shot else LhmHelper(exe, interval)
        except OSError as error:
            return NoSensors(f"sensor helper failed to start: {error}")
    if sys.platform.startswith("linux"):
        return LinuxSensors(smart=not one_shot)
    return NoSensors("temperature, fan and power sensors on macOS need SMC access, which gputaskman does not read yet")
