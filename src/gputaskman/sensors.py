"""Hardware sensors: temperatures, fans, power, voltages, clocks.

Every reading names its source:

* ``lhm``: Windows, LibreHardwareMonitor through the bundled ``gputaskman-sensors.exe`` helper. CPU, motherboard
  and memory sensors need the PawnIO driver (installed by the Windows installer, never shipped in the app folder)
  and an elevated gputaskman; without them only the sensors LibreHardwareMonitor reaches without a driver remain.
* ``hwmon``: Linux ``/sys/class/hwmon``, the kernel drivers' own readings.
* ``rapl``: Linux ``/sys/class/powercap`` energy counters. Power is the rate between two readings (``None`` on
  the first), and the counters are root-only on current kernels.
* ``battery``: psutil.

A sensor that exists but cannot be read is ``None``, never 0. These are sensor readings, never GPU bytes, and
nothing here is summed into a GPU total. Coverage gaps are explained in ``SensorReading.notes``.
"""

from __future__ import annotations

import json
import os
import re
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
KIND_ORDER = ("temperature", "power", "clock", "fan", "voltage", "current", "load", "control", "level", "frequency",
              "flow", "throughput", "data", "energy", "factor", "time", "noise", "humidity", "conductivity")
HELPER_NAME = "gputaskman-sensors.exe"
HELPER_TIMEOUT_S = 20.0  # LibreHardwareMonitor's first open enumerates SMBus, storage and GPUs; it can take seconds


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

    def text(self, value: float | None = None) -> str:
        v = self.value if value is None else value
        if v is None:
            return "n/a"
        if self.unit == "B/s":
            for unit, scale in (("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3)):
                if v >= scale:
                    return f"{v / scale:.1f} {unit}"
            return f"{v:.0f} B/s"
        if self.kind in ("fan", "clock") or self.unit in ("RPM", "MHz"):
            return f"{v:.0f} {self.unit}"
        if self.kind == "voltage":
            return f"{v:.3f} {self.unit}"
        if self.unit:
            return f"{v:.1f} {self.unit}" if self.unit != "%" else f"{v:.1f}%"
        return f"{v:.2f}"

    def to_dict(self) -> dict:
        def r(x: float | None) -> float | None:
            return round(x, 3) if x is not None else None

        return {"hardware": self.hardware, "group": self.group, "name": self.name, "kind": self.kind,
                "value": r(self.value), "unit": self.unit, "min": r(self.min), "max": r(self.max), "source": self.source}


@dataclass
class SensorReading:
    sensors: list[Sensor]
    notes: list[str] = field(default_factory=list)  # why coverage is partial: no driver, not elevated, root-only
    source: str = ""

    def of_kind(self, kind: str) -> list[Sensor]:
        return [s for s in self.sensors if s.kind == kind]


class SensorSource:
    """Base: a source of SensorReadings. Session min/max is tracked here for sources that don't keep their own."""

    name = ""

    def __init__(self) -> None:
        self._minmax: dict[str, tuple[float, float]] = {}

    def track(self, sensors: list[Sensor]) -> list[Sensor]:
        for s in sensors:
            if s.value is None:
                continue
            lo, hi = self._minmax.get(s.key, (s.value, s.value))
            lo, hi = min(lo, s.value), max(hi, s.value)
            self._minmax[s.key] = (lo, hi)
            if s.min is None:
                s.min = lo
            if s.max is None:
                s.max = hi
        return sensors

    def read(self) -> SensorReading:
        raise NotImplementedError

    def close(self) -> None:
        pass


def _sort(sensors: list[Sensor]) -> list[Sensor]:
    order = {k: i for i, k in enumerate(KIND_ORDER)}
    return sorted(sensors, key=lambda s: (s.group != "Cpu", s.hardware.lower(), order.get(s.kind, 99), s.name.lower()))


# ---------------------------------------------------------------------------------------------- battery (all OSes)

def battery_sensors() -> list[Sensor]:
    try:
        b = psutil.sensors_battery()
    except (OSError, NotImplementedError, AttributeError, RuntimeError):
        return []
    if b is None:
        return []
    out = [Sensor("battery/charge", "Battery", "Battery", "Charge", "level", float(b.percent), "%", "battery")]
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


def parse_lhm(line: str | dict) -> tuple[list[Sensor], dict]:
    """One helper JSON line -> sensors plus its status fields (admin, pawnio, error)."""
    data = json.loads(line) if isinstance(line, str) else line
    names = {hw["id"]: hw.get("name") or hw["id"] for hw in data.get("hardware", [])}
    sensors = []
    for hw in data.get("hardware", []):
        parent = hw.get("parent")
        hardware = hw.get("name") or hw.get("id", "?")
        if parent and parent in names and names[parent] != hardware:
            hardware = f"{names[parent]} / {hardware}"  # e.g. "ASUS ROG STRIX B650E-F / Nuvoton NCT6799D"
        for s in hw.get("sensors", []):
            kind, unit = LHM_TYPES.get(s.get("type", ""), (str(s.get("type", "other")).lower(), ""))
            value = s.get("value")
            sensors.append(Sensor(
                key=s.get("id") or f"{hw.get('id')}/{s.get('name')}", hardware=hardware, group=hw.get("type", ""),
                name=s.get("name", "?"), kind=kind, value=float(value) if isinstance(value, (int, float)) else None,
                unit=unit, source="lhm",
                min=float(s["min"]) if isinstance(s.get("min"), (int, float)) else None,
                max=float(s["max"]) if isinstance(s.get("max"), (int, float)) else None))
    return sensors, {"admin": bool(data.get("admin")), "pawnio": bool(data.get("pawnio")), "error": data.get("error"),
                     "t": data.get("t")}


def lhm_notes(status: dict, sensors: list[Sensor]) -> list[str]:
    notes = []
    if status.get("error"):
        notes.append(f"LibreHardwareMonitor: {status['error']}")
    if not status.get("pawnio"):
        notes.append("PawnIO driver not installed: CPU temperatures, package power, fans and voltages need it "
                     "(reinstall gputaskman with the sensor-driver option, or install PawnIO from pawnio.eu)")
    if not status.get("admin"):
        notes.append("not elevated: run gputaskman as administrator (Start menu: GPU Task Manager (administrator)) "
                     "for CPU, motherboard and memory sensors")
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
        for raw in self._proc.stdout:
            try:
                parsed = parse_lhm(raw.decode("utf-8", errors="replace"))
            except (ValueError, KeyError, TypeError) as error:
                with self._lock:
                    self._error = f"unreadable helper output ({type(error).__name__})"
                continue
            with self._lock:
                self._latest, self._error = parsed, None
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
        sensors, status = latest
        notes = lhm_notes(status, sensors) + ([error] if error else [])
        if not any(s.group == "Battery" for s in sensors):
            sensors = sensors + extra
        return SensorReading(_sort(sensors), notes, self.name)

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
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._result, self._error = None, f"sensor helper failed: {type(error).__name__}: {error}"

    def read(self) -> SensorReading:
        if self._result is None:
            return SensorReading(battery_sensors(), [self._error or "no sensor reading"], self.name)
        sensors, status = self._result
        if not any(s.group == "Battery" for s in sensors):
            sensors = sensors + battery_sensors()
        return SensorReading(_sort(sensors), lhm_notes(status, sensors), self.name)


# ---------------------------------------------------------------------------------------------- Linux: hwmon + RAPL

_HWMON_FILE = re.compile(r"^(temp|fan|in|curr|power|freq)(\d+)_(input|average)$")
_HWMON_KIND = {"temp": ("temperature", "°C", 1e-3), "fan": ("fan", "RPM", 1.0), "in": ("voltage", "V", 1e-3),
               "curr": ("current", "A", 1e-3), "power": ("power", "W", 1e-6), "freq": ("clock", "MHz", 1e-6)}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def _hwmon_device(d: Path, chip: str) -> str:
    """A readable device name: the NVMe model, the drive model, else the driver name."""
    model = _read(d / "device" / "model")
    return f"{chip} ({model})" if model else chip


class LinuxSensors(SensorSource):
    name = "hwmon + RAPL"

    def __init__(self, hwmon_root: Path = Path("/sys/class/hwmon"), rapl_root: Path = Path("/sys/class/powercap"),
                 supply_root: Path = Path("/sys/class/power_supply")) -> None:
        super().__init__()
        self._hwmon_root, self._rapl_root, self._supply_root = hwmon_root, rapl_root, supply_root
        self._rapl_last: dict[str, tuple[int, float]] = {}
        self._rapl_denied = False

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
                raw = _read(d / fname)
                try:
                    value = int(raw) * scale if raw is not None else None
                except ValueError:
                    value = None  # an unreadable sensor is unknown, not zero
                out.append(Sensor(f"hwmon/{d.name}/{prefix}{num}", device, chip, label, kind, value, unit, "hwmon"))
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
                    try:
                        delta += int((z / "max_energy_range_uj").read_text())
                    except (OSError, ValueError):
                        delta = -1
                if delta >= 0:
                    value = delta / 1e6 / (now - before[1])
            self._rapl_last[z.name] = (energy, now)
            label = {"package-0": "Package", "core": "Cores", "uncore": "Uncore/iGPU", "dram": "DRAM",
                     "psys": "Platform"}.get(name, name)
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
            raw = _read(s / "power_now")
            if raw is None:
                cur, volt = _read(s / "current_now"), _read(s / "voltage_now")
                try:
                    raw = str(int(cur) * int(volt) // 1_000_000) if cur and volt else None
                except ValueError:
                    raw = None
            try:
                watts = int(raw) / 1e6 if raw is not None else None
            except ValueError:
                watts = None
            status = _read(s / "status") or ""
            out.append(Sensor(f"battery/{s.name}/power", "Battery", "Battery",
                              f"{s.name} {'discharge' if status == 'Discharging' else 'charge'} rate", "power",
                              watts, "W", "battery"))
        return out

    def read(self) -> SensorReading:
        sensors = self._hwmon() + self._rapl() + battery_sensors() + self._battery_power()
        notes = []
        if self._rapl_denied:
            notes.append("CPU package power (RAPL) needs root: the energy counters are root-only on current kernels")
        if not any(s.source == "hwmon" for s in sensors):
            notes.append("no hwmon sensors found (containers and VMs usually expose none)")
        return SensorReading(_sort(self.track(sensors)), notes, self.name)


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
            return NoSensors("temperatures, fans and power need the sensor helper, which ships with the installer "
                             "and portable builds (not with pip/uv installs)")
        try:
            return LhmOnce(exe) if one_shot else LhmHelper(exe, interval)
        except OSError as error:
            return NoSensors(f"sensor helper failed to start: {error}")
    if sys.platform.startswith("linux"):
        return LinuxSensors()
    return NoSensors("temperature, fan and power sensors on macOS need SMC access, which gputaskman does not read yet")
