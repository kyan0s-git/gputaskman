"""The hardware view: Summary > CPU > Memory > GPU > Storage > Network > Motherboard & power > All sensors.

A navigation list on the left picks a component; the right side shows its headline numbers with 60 s graphs and an
HWiNFO-style table (Now, Min, Avg, Max, Source). Everything is OS-, driver- or sensor-reported machine telemetry
(``machine.py``, ``sensors.py``, and the GPU snapshots the main view already collects): never GPU bytes added to a
GPU total. Unknown values show ``n/a``; a rate's first reading shows ``…``; min/avg/max cover this session (``r``
resets them).

Rendering never takes the app down: a failure while drawing is caught, logged to the crash log, and shown in the
notes line, and the next refresh tries again.
"""

from __future__ import annotations

import platform
import re
from collections import deque
from dataclasses import dataclass

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, OptionList, Static
from textual.widgets.option_list import Option

from .machine import HardwareSample
from .sensors import KIND_ORDER, KIND_TITLES, DiskHealth, Sensor, SensorReading, component_of, finite, fmt

SPARK = "▁▂▃▄▅▆▇█"
SENSOR_FILTERS = ("all", "temperature", "power", "fan", "voltage", "clock", "load")
CPU_TEMP_NAMES = ("cpu package", "core (tctl/tdie)", "tctl/tdie", "tctl", "tdie", "package id 0", "core max",
                  "cpu", "cpu die (average)", "core average")
COLUMNS = ("Sensor", "Now", "Min", "Avg", "Max", "Source")
SECTIONS = ("Overview", "Processor", "Memory", "Graphics", "Storage", "Network", "Motherboard & power", "Sensors")


# ---------------------------------------------------------------------------------------------- formatting

def num(v) -> float | None:
    return finite(v)


def spark(values, width: int, top: float | None = None) -> str:
    width = max(1, int(width))
    vals = [num(v) for v in list(values)[-width:]]
    known = [v for v in vals if v is not None]
    peak = top if top else (max(known) if known else 1.0)
    if not peak or peak <= 0:
        peak = 1.0
    return "".join(SPARK[min(max(int(v / peak * (len(SPARK) - 1) + 0.5), 0), len(SPARK) - 1)] if v is not None else " "
                   for v in vals)


def bar(value, total, width: int, style: str) -> Text:
    width = max(1, int(width))
    t = Text()
    value, total = num(value), num(total)
    if value is None or not total or total <= 0:
        t.append("·" * width, style="grey27")
        return t
    n = max(0, min(width, int(round(value / total * width))))
    t.append("█" * n, style=style)
    t.append("░" * (width - n), style="grey27")
    return t


def pct_style(v) -> str:
    v = num(v)
    return "grey50" if v is None else "red" if v >= 90 else "yellow" if v >= 60 else "green3"


def temp_style(v) -> str:
    v = num(v)
    return "grey50" if v is None else "red" if v >= 90 else "dark_orange" if v >= 75 else "yellow" if v >= 60 else "green3"


def gb(n) -> str:
    n = num(n)
    return "n/a" if n is None else f"{n / 2**30:.1f} GB"


def rate(bps) -> str:
    return "…" if num(bps) is None else fmt("throughput", "B/s", bps)


def bits(bps) -> str:
    bps = num(bps)
    if bps is None:
        return "…"
    b = bps * 8
    for unit, scale in (("Gb/s", 1e9), ("Mb/s", 1e6), ("Kb/s", 1e3)):
        if abs(b) >= scale:
            return f"{b / scale:.1f} {unit}"
    return f"{b:.0f} b/s"


def uptime(seconds) -> str:
    seconds = num(seconds)
    if seconds is None or seconds < 0:
        return "n/a"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"


# Formatters by metric kind: (kind, unit) for fmt(), or a callable.
FORMATS = {
    "pct": lambda v: fmt("load", "%", v), "mhz": lambda v: fmt("clock", "MHz", v), "temp": lambda v: fmt("temperature", "°C", v),
    "watts": lambda v: fmt("power", "W", v), "bytes": gb, "rate": rate, "bits": bits, "rpm": lambda v: fmt("fan", "RPM", v),
    "count": lambda v: "n/a" if num(v) is None else f"{num(v):,.0f}",
}


# ---------------------------------------------------------------------------------------------- statistics

class Stat:
    __slots__ = ("min", "max", "total", "n", "last")

    def __init__(self) -> None:
        self.min = self.max = self.last = None
        self.total, self.n = 0.0, 0

    def add(self, v) -> None:
        v = num(v)
        self.last = v
        if v is None:
            return
        self.min = v if self.min is None else min(self.min, v)
        self.max = v if self.max is None else max(self.max, v)
        self.total += v
        self.n += 1

    @property
    def avg(self) -> float | None:
        return self.total / self.n if self.n else None


class HardwareHistory:
    """Per-metric 60-sample series (sparklines) and session min/avg/max, kept by the app across visits to the view."""

    def __init__(self, n: int = 60) -> None:
        self.n = n
        self.series: dict[str, deque] = {}
        self.stats: dict[str, Stat] = {}
        self._reset = False

    def reset(self) -> None:
        self._reset = True

    def push(self, key: str, v) -> None:
        series = self.series.get(key)
        if series is None:
            series = self.series[key] = deque(maxlen=self.n)
        series.append(num(v))
        stat = self.stats.get(key)
        if stat is None:
            stat = self.stats[key] = Stat()
        stat.add(v)

    def stat(self, key: str) -> Stat:
        return self.stats.get(key) or Stat()

    def get(self, key: str) -> deque:
        return self.series.get(key) or deque()

    # Kept for the main view's System panel and older callers.
    @property
    def cpu(self) -> deque:
        return self.get("cpu.util")

    @property
    def temp(self) -> deque:
        return self.get("cpu.temp")

    @property
    def power(self) -> deque:
        return self.get("cpu.power")

    def add(self, h: HardwareSample, snapshots=()) -> None:
        if self._reset:
            self.stats, self._reset = {}, False
        c, m = h.cpu, h.memory
        self.push("cpu.util", c.util_pct)
        self.push("cpu.mhz", c.speed_mhz)
        for i, v in enumerate(c.per_core_pct):
            self.push(f"core{i}.util", v)
        for i, v in enumerate(c.per_core_mhz):
            self.push(f"core{i}.mhz", v)
        for key, v in (("processes", c.processes), ("threads", c.threads), ("handles", c.handles)):
            self.push(f"cpu.{key}", v)
        temp, power = cpu_temperature(h.sensors), package_power(h.sensors)
        self.push("cpu.temp", temp.value if temp else None)
        self.push("cpu.power", power.value if power else None)
        self.push("ram.used", m.used)
        self.push("ram.pct", 100 * m.used / m.total if m.total else None)
        self.push("ram.available", m.available)
        self.push("ram.cached", m.cached)
        self.push("ram.committed", m.committed)
        self.push("ram.swap", m.swap_used if m.swap_total else None)
        for d in h.disks:
            self.push(f"disk.{d.name}.read", d.read_bps)
            self.push(f"disk.{d.name}.write", d.write_bps)
            self.push(f"disk.{d.name}.active", d.active_pct)
        for n in h.nics:
            self.push(f"nic.{n.name}.rx", n.rx_bps)
            self.push(f"nic.{n.name}.tx", n.tx_bps)
        for i, snap in enumerate(snapshots or ()):
            nv = getattr(snap, "nv", None)
            for key, attr in (("util", "util_gpu"), ("memutil", "util_mem"), ("temp", "temp"), ("power", "power_w"),
                              ("sm", "sm_clock"), ("gfx", "gfx_clock"), ("mem", "mem_clock"), ("fan", "fan"),
                              ("enc", "enc"), ("dec", "dec")):
                self.push(f"gpu{i}.{key}", getattr(nv, attr, None) if nv else None)
            self.push(f"gpu{i}.vram", getattr(snap, "vram_used", None))


# ---------------------------------------------------------------------------------------------- sensor picks

def cpu_temperature(reading: SensorReading) -> Sensor | None:
    """The CPU's headline temperature: package/Tctl first, then the hottest CPU temperature."""
    temps = [s for s in reading.sensors if s.kind == "temperature" and num(s.value) is not None and component_of(s) == "cpu"]
    for wanted in CPU_TEMP_NAMES:
        for s in temps:
            if s.name.lower() == wanted:
                return s
    return max(temps, key=lambda s: s.value, default=None)


def package_power(reading: SensorReading) -> Sensor | None:
    power = [s for s in reading.sensors if s.kind == "power" and num(s.value) is not None and component_of(s) == "cpu"
             and "limit" not in s.name.lower()]
    for s in power:
        if s.name.lower() in ("package", "cpu package", "package power"):
            return s
    return power[0] if power else None


def fans(reading: SensorReading) -> list[Sensor]:
    """Fans that are turning: a stopped fan (0 RPM, often an empty header) is a reading, not a spinning fan."""
    return [s for s in reading.sensors if s.kind == "fan" and num(s.value) is not None and s.value > 0]


def tjmax(reading: SensorReading) -> tuple[float | None, str]:
    """TjMax as reported (coretemp's tempN_crit) or derived (LHM's "Distance to TjMax" + the core temperature)."""
    cpu = [s for s in reading.sensors if component_of(s) == "cpu" and s.kind == "temperature"]
    for s in cpu:
        if s.source == "hwmon" and num(s.limit) is not None and s.group == "coretemp":
            return s.limit, "reported by coretemp (critical limit)"
    distances = {re.sub(r"\s*distance to tjmax", "", s.name, flags=re.I).strip().lower(): s for s in cpu
                 if "distance to tjmax" in s.name.lower() and num(s.value) is not None}
    for s in cpu:
        d = distances.get(s.name.strip().lower())
        if d is not None and num(s.value) is not None:
            return s.value + d.value, f"derived: {s.name} + its distance to TjMax (LibreHardwareMonitor)"
    if any(s.group in ("k10temp", "zenpower") for s in cpu) or any("tctl" in s.name.lower() for s in cpu):
        return None, "AMD does not report TjMax to the OS (Tctl/Tdie is the control temperature)"
    return None, "not reported for this processor"


def power_limit(reading: SensorReading) -> tuple[Sensor | None, str]:
    for s in reading.sensors:
        if component_of(s) == "cpu" and s.kind == "power" and ("pl1" in s.name.lower() or "tdp" in s.name.lower()
                                                                  or "ppt limit" in s.name.lower()):
            return s, ""
    return None, ("not reported (Linux shows RAPL PL1; on Windows LibreHardwareMonitor exposes no power limit "
                  "for most processors)")


def sensor_summary(h: HardwareSample) -> Text | None:
    """One line for the main System panel: CPU temperature, package power, fastest fan."""
    t = Text(no_wrap=True, overflow="ellipsis")
    temp, power, spinning = cpu_temperature(h.sensors), package_power(h.sensors), fans(h.sensors)
    if temp is None and power is None and not spinning:
        return None
    t.append("     ")
    if temp is not None:
        t.append("CPU ", style="grey62")
        t.append(f"{temp.value:.0f}°C", style=temp_style(temp.value))
        t.append("  ")
    if power is not None:
        t.append("package ", style="grey62")
        t.append(f"{power.value:.0f} W", style="bold yellow")
        t.append("  ")
    if spinning:
        top = max(spinning, key=lambda s: s.value)
        t.append("fans ", style="grey62")
        t.append(f"{len(spinning)} spinning, max {top.value:.0f} RPM")
        t.append("  ")
    t.append("h: hardware view", style="grey50")
    return t


# ---------------------------------------------------------------------------------------------- pages

@dataclass
class Row:
    label: str
    now: str = ""
    min: str = ""
    avg: str = ""
    max: str = ""
    source: str = ""
    style: str = ""
    header: bool = False


@dataclass
class Page:
    id: str
    section: str
    label: str


def natural(text: str) -> tuple:
    """Core #2 before Core #10."""
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", (text or "").lower()))


def short(text: str, n: int = 30) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _model_match(model: str | None, hardware: str) -> bool:
    if not model:
        return False
    a, b = _norm(model), _norm(hardware)
    return bool(a) and (a in b or b in a)


def disk_health_for(disk, health: list[DiskHealth]) -> DiskHealth | None:
    for d in health:
        if d.index is None:
            continue
        if disk.name == f"PhysicalDrive{d.index}" or disk.name == d.index or (
                d.index.startswith("nvme") and disk.name.startswith(d.index + "n")):
            return d
    return None


class Model:
    """Turns one HardwareSample (plus the GPU snapshots) into pages and rows. Pure: no widgets, easy to stress."""

    def __init__(self, h: HardwareSample, snapshots, history: HardwareHistory) -> None:
        self.h, self.snaps, self.hist = h, list(snapshots or ()), history
        self.by_comp: dict[str, list[Sensor]] = {}
        for s in h.sensors.sensors:
            self.by_comp.setdefault(component_of(s), []).append(s)
        # Drives: psutil's disks, with their model and health, and the storage sensors that match one model only.
        self.disk_models = {}
        for d in h.disks:
            health = disk_health_for(d, h.sensors.health)
            self.disk_models[d.name] = d.model or (health.model if health else None)
        self.disk_sensors: dict[str, list[Sensor]] = {}
        self.loose_storage: list[Sensor] = []
        for s in self.by_comp.get("storage", []):
            matches = [name for name, model in self.disk_models.items() if _model_match(model, s.hardware)]
            same_model = [hw for hw in {x.hardware for x in self.by_comp.get("storage", [])} if
                          matches and _model_match(self.disk_models[matches[0]], hw)]
            if len(matches) == 1 and len(same_model) == 1:
                self.disk_sensors.setdefault(matches[0], []).append(s)
            else:
                self.loose_storage.append(s)  # two drives of one model: we can't tell which sensor is whose
        # GPUs: snapshots from the main view, and LibreHardwareMonitor/hwmon GPU sensors matched by name or order.
        gpu_hw = []
        for s in self.by_comp.get("gpu", []):
            if s.hardware not in gpu_hw:
                gpu_hw.append(s.hardware)
        self.gpu_sensors: dict[int, list[Sensor]] = {}
        self.loose_gpu: list[Sensor] = []
        names = [self.gpu_name(i) for i in range(len(self.snaps))]
        for hw in gpu_hw:
            idx = [i for i, n in enumerate(names) if _model_match(n, hw)]
            if len(idx) != 1 and len(gpu_hw) == len(self.snaps) == 1:
                idx = [0]
            group = [s for s in self.by_comp.get("gpu", []) if s.hardware == hw]
            if len(idx) == 1:
                self.gpu_sensors.setdefault(idx[0], []).extend(group)
            else:
                self.loose_gpu.extend(group)

    def gpu_name(self, i: int) -> str:
        snap = self.snaps[i]
        adapter, nv = getattr(snap, "adapter", None), getattr(snap, "nv", None)
        return str(getattr(adapter, "name", None) or getattr(nv, "name", None) or f"GPU {i}")

    def pages(self) -> list[Page]:
        h = self.h
        out = [Page("summary", "Overview", "Summary")]
        model = h.cpu.model or "CPU"
        out.append(Page("cpu", "Processor", f"CPU  {model}"))
        out.append(Page("memory", "Memory", f"Memory  {gb(h.memory.total)}"))
        for i in range(len(self.snaps)):
            out.append(Page(f"gpu:{i}", "Graphics", f"GPU {i}  {self.gpu_name(i)}"))
        if self.loose_gpu:
            out.append(Page("gpu:other", "Graphics", "Other GPU sensors"))
        for i, d in enumerate(h.disks):
            out.append(Page(f"disk:{i}", "Storage", f"{d.name}  {self.disk_models.get(d.name) or ''}".strip()))
        if h.volumes:
            out.append(Page("volumes", "Storage", f"Volumes ({len(h.volumes)})"))
        if self.loose_storage:
            out.append(Page("drives:other", "Storage", "Drive sensors"))
        for i, n in enumerate(h.nics):
            out.append(Page(f"nic:{i}", "Network", f"{n.name}{'' if n.up else '  (down)'}"))
        if self.by_comp.get("network"):
            out.append(Page("nic:sensors", "Network", "Adapter sensors"))
        out.append(Page("board", "Motherboard & power", "Motherboard"))
        if self.by_comp.get("battery"):
            out.append(Page("battery", "Motherboard & power", "Battery"))
        out.append(Page("sensors", "Sensors", f"All sensors ({len(h.sensors.sensors)})"))
        return out

    # -- row helpers

    def metric(self, label: str, key: str, value, kind: str, source: str, style_fn=None) -> Row:
        f = FORMATS[kind]
        st = self.hist.stat(key)
        return Row(label, f(value), f(st.min) if st.n else "", f(st.avg) if st.n else "", f(st.max) if st.n else "",
                   source, style_fn(value) if style_fn else "")

    @staticmethod
    def sensor_row(s: Sensor, label: str | None = None) -> Row:
        style = temp_style(s.value) if s.kind == "temperature" else ""
        stats = s.min is not None
        return Row(label or s.name, s.text(), s.text(s.min) if stats else "", s.text(s.avg) if s.avg is not None else "",
                   s.text(s.max) if stats else "", s.source, style)

    @staticmethod
    def static(label: str, text: str, source: str = "", style: str = "") -> Row:
        return Row(label, text, "", "", "", source, style)

    def sensor_rows(self, sensors: list[Sensor], by_hardware: bool = False) -> list[Row]:
        """Sensors grouped by kind (Temperatures, Power, ...) or by hardware then kind."""
        rows: list[Row] = []
        order = {k: i for i, k in enumerate(KIND_ORDER)}
        if by_hardware:
            hardware = []
            for s in sensors:
                if s.hardware not in hardware:
                    hardware.append(s.hardware)
            for hw in hardware:
                rows.append(Row(hw, header=True))
                for s in sorted((s for s in sensors if s.hardware == hw), key=lambda s: (order.get(s.kind, 99), natural(s.name))):
                    rows.append(self.sensor_row(s, f"  {s.name}"))
            return rows
        kinds = sorted({s.kind for s in sensors}, key=lambda k: order.get(k, 99))
        for kind in kinds:
            rows.append(Row(KIND_TITLES.get(kind, kind.title()), header=True))
            group = sorted((s for s in sensors if s.kind == kind), key=lambda s: (natural(s.hardware), natural(s.name)))
            several = len({s.hardware for s in group}) > 1
            for s in group:
                rows.append(self.sensor_row(s, f"  {s.name}" + (f"  ({s.hardware})" if several else "")))
        return rows

    # -- pages: (header renderables, rows)

    def build(self, page_id: str, width: int, filt: str = "all"):
        builder = page_id.split(":", 1)[0]
        return getattr(self, f"page_{builder.replace('.', '_')}")(page_id, max(30, width), filt)

    def page_summary(self, _id, w, _f):
        h, c, m = self.h, self.h.cpu, self.h.memory
        lines = [Text(f"{c.model} · {c.logical} threads" + (f" / {c.physical} cores" if c.physical else "")
                      + f" · {gb(m.total)} RAM · {platform.system()} {platform.release()} · up {uptime(c.uptime_s)}",
                      style="bold", no_wrap=True, overflow="ellipsis")]
        grid = Table.grid(padding=(0, 1))
        for _ in range(4):
            grid.add_column(no_wrap=True)
        bw = max(8, min(40, w // 3))
        temp, power = cpu_temperature(h.sensors), package_power(h.sensors)
        extra = " ".join(x for x in (f"{temp.value:.0f}°C" if temp else "", f"{power.value:.0f} W" if power else "") if x)
        grid.add_row(Text("CPU", style="bold green3"), bar(c.util_pct, 100, bw, pct_style(c.util_pct)),
                     Text(FORMATS["pct"](c.util_pct)), Text(extra, style=temp_style(temp.value if temp else None)))
        grid.add_row(Text("Memory", style="bold dodger_blue2"), bar(m.used, m.total, bw, "dodger_blue2"),
                     Text(f"{gb(m.used)} / {gb(m.total)}"), Text(""))
        for i, snap in enumerate(self.snaps):
            nv = getattr(snap, "nv", None)
            util = getattr(nv, "util_gpu", None) if nv else None
            t = getattr(nv, "temp", None) if nv else None
            pw = getattr(nv, "power_w", None) if nv else None
            grid.add_row(Text(f"GPU {i}", style="bold magenta"), bar(util, 100, bw, pct_style(util)),
                         Text(FORMATS["pct"](util)),
                         Text(" ".join(x for x in (f"{t:.0f}°C" if num(t) is not None else "",
                                                    f"{pw:.0f} W" if num(pw) is not None else "") if x)))
            used, total = getattr(snap, "vram_used", None), getattr(snap, "vram_total", None)
            grid.add_row(Text("  VRAM", style="magenta"), bar(used, total, bw, "magenta"),
                         Text(f"{gb(used)} / {gb(total)}"), Text(""))
        for d in h.disks:
            grid.add_row(Text(d.name, style="bold dark_orange", no_wrap=True, overflow="ellipsis"),
                         bar(d.active_pct, 100, bw, pct_style(d.active_pct)),
                         Text(f"R {rate(d.read_bps)}  W {rate(d.write_bps)}"), Text(""))
        for n in [n for n in h.nics if n.up][:6]:
            grid.add_row(Text(n.name, style="bold cyan", no_wrap=True, overflow="ellipsis"), Text(""),
                         Text(f"↓ {bits(n.rx_bps)}  ↑ {bits(n.tx_bps)}"), Text(""))
        lines.append(grid)
        rows = [Row("Processor", header=True),
                self.metric("  CPU load", "cpu.util", c.util_pct, "pct", "os", pct_style),
                self.metric("  CPU speed", "cpu.mhz", c.speed_mhz, "mhz", "os"),
                self.metric("  CPU temperature" + (f" ({temp.name})" if temp else ""), "cpu.temp",
                            temp.value if temp else None, "temp", temp.source if temp else "", temp_style),
                self.metric("  CPU package power", "cpu.power", power.value if power else None, "watts",
                            power.source if power else ""),
                Row("Memory", header=True),
                self.metric("  Used", "ram.used", m.used, "bytes", "os"),
                self.metric("  Used %", "ram.pct", 100 * m.used / m.total if m.total else None, "pct", "os", pct_style)]
        for i, snap in enumerate(self.snaps):
            nv = getattr(snap, "nv", None)
            src = getattr(nv, "source", "") if nv else ""
            rows += [Row(f"GPU {i}  {self.gpu_name(i)}", header=True),
                     self.metric("  Load", f"gpu{i}.util", getattr(nv, "util_gpu", None), "pct", src, pct_style),
                     self.metric("  Temperature", f"gpu{i}.temp", getattr(nv, "temp", None), "temp", src, temp_style),
                     self.metric("  Power", f"gpu{i}.power", getattr(nv, "power_w", None), "watts", src),
                     self.metric("  VRAM used", f"gpu{i}.vram", getattr(snap, "vram_used", None), "bytes", src)]
        if h.disks:
            rows.append(Row("Storage", header=True))
            for d in h.disks:
                rows += [self.metric(f"  {d.name} read", f"disk.{d.name}.read", d.read_bps, "rate", "os"),
                         self.metric(f"  {d.name} write", f"disk.{d.name}.write", d.write_bps, "rate", "os")]
            for d in h.sensors.health:
                rows.append(self.static(f"  {d.model} health", d.health or "unknown", d.source, health_style(d.health)))
        if any(n.up for n in h.nics):
            rows.append(Row("Network", header=True))
            for n in [n for n in h.nics if n.up]:
                rows += [self.metric(f"  {n.name} down", f"nic.{n.name}.rx", n.rx_bps, "bits", "os"),
                         self.metric(f"  {n.name} up", f"nic.{n.name}.tx", n.tx_bps, "bits", "os")]
        board = [s for s in self.by_comp.get("board", []) if s.kind in ("temperature", "fan") and num(s.value) is not None]
        if board:
            rows.append(Row("Motherboard", header=True))
            hottest = max((s for s in board if s.kind == "temperature"), key=lambda s: s.value, default=None)
            if hottest:
                rows.append(self.sensor_row(hottest, f"  Hottest: {hottest.name}"))
            for s in fans(SensorReading(board)):
                rows.append(self.sensor_row(s, f"  {s.name}"))
        return lines, rows

    def page_cpu(self, _id, w, _f):
        h, c = self.h, self.h.cpu
        temp, power = cpu_temperature(h.sensors), package_power(h.sensors)
        tj, tj_note = tjmax(h.sensors)
        limit, limit_note = power_limit(h.sensors)
        sensors = self.by_comp.get("cpu", [])
        vid = [s for s in sensors if s.kind == "voltage" and ("vid" in s.name.lower() or "core" in s.name.lower())]
        head = [Text(c.model, style="bold", no_wrap=True, overflow="ellipsis")]
        info = Table.grid(padding=(0, 2))
        for _ in range(4):
            info.add_column(no_wrap=True)
        info.add_row(Text("Load", style="grey62"), Text(FORMATS["pct"](c.util_pct), style="bold " + pct_style(c.util_pct)),
                     Text("Speed", style="grey62"), Text(FORMATS["mhz"](c.speed_mhz), style="bold"))
        info.add_row(Text("Temperature", style="grey62"),
                     Text(f"{temp.value:.1f} °C ({temp.name})" if temp else "n/a", style=temp_style(temp.value if temp else None)),
                     Text("Package power", style="grey62"), Text(f"{power.value:.1f} W" if power else "n/a", style="bold yellow"))
        info.add_row(Text("TjMax", style="grey62"), Text(f"{tj:.0f} °C" if tj is not None else "n/a"),
                     Text("TDP / PL1", style="grey62"), Text(limit.text() if limit else "n/a"))
        vcore = vid[0] if vid else None
        info.add_row(Text("Core voltage / VID", style="grey62"), Text(f"{vcore.text()} ({vcore.name})" if vcore else "n/a"),
                     Text("Threads / cores", style="grey62"),
                     Text(f"{c.logical} / {c.physical}" if c.physical else str(c.logical)))
        info.add_row(Text("Processes", style="grey62"), Text(FORMATS["count"](c.processes)),
                     Text("Threads · handles", style="grey62"),
                     Text(f"{FORMATS['count'](c.threads)} · {FORMATS['count'](c.handles)}"))
        info.add_row(Text("Uptime", style="grey62"), Text(uptime(c.uptime_s)),
                     Text("Load average", style="grey62"),
                     Text(" ".join(f"{v:.2f}" for v in c.load_avg) if c.load_avg else "n/a (Windows has none)"))
        head.append(info)
        gw = max(10, w // 2 - 12)
        graph = Text("Load ", style="grey62")
        graph.append(spark(self.hist.get("cpu.util"), gw, 100), style="green3")
        graph.append("  Temp ", style="grey62")
        graph.append(spark(self.hist.get("cpu.temp"), gw, 110), style="red")
        head.append(graph)
        # Task Manager's logical-processor grid: one small bar per logical CPU.
        cols = max(1, min(16, (w - 2) // 9))
        for start in range(0, len(c.per_core_pct), cols):
            row = Text()
            for i in range(start, min(start + cols, len(c.per_core_pct))):
                v = num(c.per_core_pct[i])
                row.append(SPARK[min(int((v or 0) / 100 * (len(SPARK) - 1) + 0.5), len(SPARK) - 1)] if v is not None else "·",
                           style=pct_style(v))
                row.append(f"{i:>3}:{v:3.0f}% " if v is not None else f"{i:>3}:  …  ", style="grey62")
            head.append(row)
        notes = []
        if tj is None:
            notes.append(f"TjMax: {tj_note}")
        elif tj_note:
            notes.append(f"TjMax {tj_note}")
        if limit is None:
            notes.append(f"TDP / PL1: {limit_note}")
        if not vid:
            notes.append("VID: not reported (Windows: LibreHardwareMonitor with PawnIO, elevated; Linux exposes none)")
        if c.speed_note:
            notes.append(c.speed_note)
        if notes:
            head.append(Text("  ·  ".join(notes), style="grey50"))
        rows = [Row("Utilization", header=True), self.metric("  Total", "cpu.util", c.util_pct, "pct", "os", pct_style)]
        for i, v in enumerate(c.per_core_pct):
            rows.append(self.metric(f"  CPU {i}", f"core{i}.util", v, "pct", "os", pct_style))
        rows += [Row("Effective clocks (OS)", header=True), self.metric("  Average", "cpu.mhz", c.speed_mhz, "mhz", "os")]
        src = "pdh" if c.base_mhz else "cpufreq"
        for i, v in enumerate(c.per_core_mhz):
            rows.append(self.metric(f"  CPU {i}", f"core{i}.mhz", v, "mhz", src))
        if tj is not None:
            rows += [Row("Limits", header=True), self.static("  TjMax", f"{tj:.0f} °C", "derived" if "derived" in tj_note else "hwmon")]
        rows += self.sensor_rows(sensors)
        return head, rows

    def page_memory(self, _id, w, _f):
        m = self.h.memory
        width = max(10, w - 40)
        head = []
        used = Text("In use     ")
        used.append_text(bar(m.used, m.total, width, "dodger_blue2"))
        used.append(f" {gb(m.used)} / {gb(m.total)}", style="bold")
        head.append(used)
        if m.committed is not None:
            commit = Text("Committed  ")
            commit.append_text(bar(m.committed, m.commit_limit, width, "medium_purple1"))
            commit.append(f" {gb(m.committed)} / {gb(m.commit_limit)}")
            head.append(commit)
        graph = Text("60 s       ", style="grey62")
        graph.append(spark(self.hist.get("ram.pct"), width, 100), style="dodger_blue2")
        head.append(graph)
        rows = [Row("System memory (OS)", header=True),
                self.metric("  In use", "ram.used", m.used, "bytes", "os"),
                self.metric("  In use %", "ram.pct", 100 * m.used / m.total if m.total else None, "pct", "os", pct_style),
                self.metric("  Available", "ram.available", m.available, "bytes", "os"),
                self.metric("  Cached", "ram.cached", m.cached, "bytes", "os"),
                self.metric("  Committed", "ram.committed", m.committed, "bytes", "os"),
                self.static("  Commit limit", gb(m.commit_limit), "os"),
                self.static("  Total", gb(m.total), "os")]
        if m.swap_total:
            rows.append(self.metric("  Swap / pagefile used", "ram.swap", m.swap_used, "bytes", "os"))
        mem = self.by_comp.get("memory", [])
        dimm_temps = [s for s in mem if s.kind == "temperature"]
        if mem:
            rows += self.sensor_rows(mem, by_hardware=True)
        if not dimm_temps:
            head.append(Text("RAM temperature: not reported (needs DIMMs with a thermal sensor; Windows: PawnIO + "
                             "elevated; Linux: the jc42 or spd5118 driver)", style="grey50"))
        return head, rows

    def page_gpu(self, page_id, w, _f):
        key = page_id.split(":", 1)[1]
        if key == "other":
            return [Text("GPU sensors not matched to a listed GPU", style="bold")], self.sensor_rows(self.loose_gpu, True)
        i = int(key)
        if i >= len(self.snaps):
            return [Text("This GPU is no longer listed.", style="yellow")], []
        snap = self.snaps[i]
        nv = getattr(snap, "nv", None)
        src = getattr(nv, "source", "") if nv else ""
        used, total = getattr(snap, "vram_used", None), getattr(snap, "vram_total", None)
        head = [Text(self.gpu_name(i), style="bold", no_wrap=True, overflow="ellipsis")]
        driver = getattr(snap, "driver", "") or ""
        if driver:
            head.append(Text(f"Driver {driver}", style="grey62"))
        vram = Text("VRAM  ")
        vram.append_text(bar(used, total, max(10, w - 40), "magenta"))
        vram.append(f" {gb(used)} / {gb(total)}", style="bold")
        head.append(vram)
        gw = max(10, w // 2 - 12)
        graph = Text("Load  ", style="grey62")
        graph.append(spark(self.hist.get(f"gpu{i}.util"), gw, 100), style="green3")
        graph.append("  Temp ", style="grey62")
        graph.append(spark(self.hist.get(f"gpu{i}.temp"), gw, 110), style="red")
        head.append(graph)
        throttle = getattr(nv, "throttle", None) if nv else None
        if throttle:
            head.append(Text("Throttling: " + ", ".join(map(str, throttle)), style="yellow"))
        rows = [Row("Load", header=True),
                self.metric("  GPU", f"gpu{i}.util", getattr(nv, "util_gpu", None), "pct", src, pct_style),
                self.metric("  Memory controller", f"gpu{i}.memutil", getattr(nv, "util_mem", None), "pct", src, pct_style),
                self.metric("  Encoder", f"gpu{i}.enc", getattr(nv, "enc", None), "pct", src),
                self.metric("  Decoder", f"gpu{i}.dec", getattr(nv, "dec", None), "pct", src),
                Row("Memory", header=True),
                self.metric("  VRAM used", f"gpu{i}.vram", used, "bytes", src),
                self.static("  VRAM total", gb(total), src),
                self.static("  Shared (system RAM)", gb(getattr(snap, "shared_used", None)), src),
                Row("Clocks", header=True),
                self.metric("  Graphics / SM", f"gpu{i}.sm", getattr(nv, "sm_clock", None) or getattr(nv, "gfx_clock", None),
                            "mhz", src),
                self.metric("  Memory", f"gpu{i}.mem", getattr(nv, "mem_clock", None), "mhz", src),
                Row("Thermals and power", header=True),
                self.metric("  Temperature", f"gpu{i}.temp", getattr(nv, "temp", None), "temp", src, temp_style),
                self.metric("  Power", f"gpu{i}.power", getattr(nv, "power_w", None), "watts", src),
                self.static("  Power limit", FORMATS["watts"](getattr(nv, "power_limit_w", None)), src),
                self.metric("  Fan", f"gpu{i}.fan", getattr(nv, "fan", None), "pct", src)]
        gen, width = getattr(nv, "pcie_gen", None), getattr(nv, "pcie_width", None)
        if gen or width:
            rows.append(self.static("  PCIe link", f"Gen {gen or '?'} x{width or '?'}", src))
        extra = self.gpu_sensors.get(i, [])
        if extra:
            rows += [Row("More sensors (LibreHardwareMonitor / hwmon)", header=True)] + self.sensor_rows(extra)
        return head, rows

    def page_disk(self, page_id, w, _f):
        i = int(page_id.split(":", 1)[1])
        if i >= len(self.h.disks):
            return [Text("This drive is no longer listed.", style="yellow")], []
        d = self.h.disks[i]
        health = disk_health_for(d, self.h.sensors.health)
        head = [Text(f"{d.name}  {self.disk_models.get(d.name) or ''}", style="bold", no_wrap=True, overflow="ellipsis")]
        if health:
            facts = [x for x in (health.media, health.bus, gb(health.size) if health.size else None) if x]
            head.append(Text(" · ".join(facts), style="grey62"))
            status = Text("Health: ")
            status.append(health.health or "unknown", style=health_style(health.health))
            if health.status:
                status.append(f"  ({health.status})", style="grey62")
            status.append(f"  source: {health.source}", style="grey50")
            head.append(status)
        else:
            head.append(Text("Health: not available (Windows: the sensor helper; Linux: smartmontools as root)", style="grey50"))
        active = Text("Active ")
        active.append_text(bar(d.active_pct, 100, max(10, w - 30), pct_style(d.active_pct)))
        active.append(f" {FORMATS['pct'](d.active_pct)}")
        head.append(active)
        gw = max(10, w // 2 - 12)
        graph = Text("Read  ", style="grey62")
        graph.append(spark(self.hist.get(f"disk.{d.name}.read"), gw), style="cyan")
        graph.append("  Write ", style="grey62")
        graph.append(spark(self.hist.get(f"disk.{d.name}.write"), gw), style="magenta")
        head.append(graph)
        rows = [Row("Activity (OS)", header=True),
                self.metric("  Read", f"disk.{d.name}.read", d.read_bps, "rate", "os"),
                self.metric("  Write", f"disk.{d.name}.write", d.write_bps, "rate", "os"),
                self.metric("  Active time", f"disk.{d.name}.active", d.active_pct, "pct", "os", pct_style),
                self.static("  Read since boot", gb(d.read_bytes), "os"),
                self.static("  Written since boot", gb(d.write_bytes), "os")]
        if health:
            src = health.source
            rows += [Row("Health (S.M.A.R.T. / reliability counters)", header=True),
                     self.static("  Status", health.health or "unknown", src, health_style(health.health)),
                     self.static("  Temperature", fmt("temperature", "°C", health.temperature), src, temp_style(health.temperature)),
                     self.static("  Highest temperature", fmt("temperature", "°C", health.temperature_max), src),
                     self.static("  Power-on hours", FORMATS["count"](health.power_on_hours), src),
                     self.static("  Life used (wear)", fmt("level", "%", health.wear_pct), src),
                     self.static("  Read errors", FORMATS["count"](health.read_errors), src),
                     self.static("  Write errors", FORMATS["count"](health.write_errors), src),
                     self.static("  Reallocated sectors", FORMATS["count"](health.reallocated), src),
                     self.static("  Pending sectors", FORMATS["count"](health.pending), src),
                     self.static("  Media errors", FORMATS["count"](health.media_errors), src)]
        own = self.disk_sensors.get(d.name, [])
        if own:
            rows += self.sensor_rows(own)
        return head, rows

    def page_volumes(self, _id, w, _f):
        table = Table.grid(padding=(0, 1))
        for _ in range(4):
            table.add_column(no_wrap=True)
        bw = max(8, w - 60)
        for v in self.h.volumes[:64]:
            low = v.total and v.free / v.total < 0.1
            table.add_row(Text(v.mount, overflow="ellipsis"), Text(v.fstype, style="grey62"),
                          bar(v.used, v.total, bw, "red" if low else "dark_cyan"),
                          Text(f"{gb(v.free)} free of {gb(v.total)}", style="red" if low else "grey62"))
        rows = []
        for v in self.h.volumes:
            rows.append(Row(f"{v.mount}  ({v.device})", header=True))
            rows += [self.static("  Used", gb(v.used), "os"), self.static("  Free", gb(v.free), "os"),
                     self.static("  Used %", fmt("load", "%", 100 * v.used / v.total if v.total else None), "os")]
        return [Text("Volumes", style="bold"), table], rows

    def page_drives(self, _id, _w, _f):
        return ([Text("Drive sensors", style="bold"),
                 Text("These could not be tied to one drive (two drives of the same model, or no matching model name).",
                      style="grey50")], self.sensor_rows(self.loose_storage, by_hardware=True))

    def page_nic(self, page_id, w, _f):
        key = page_id.split(":", 1)[1]
        if key == "sensors":
            return [Text("Network adapter sensors", style="bold")], self.sensor_rows(self.by_comp.get("network", []), True)
        i = int(key)
        if i >= len(self.h.nics):
            return [Text("This adapter is no longer listed.", style="yellow")], []
        n = self.h.nics[i]
        link = (f"{n.speed_mbps / 1000:g} Gb/s" if n.speed_mbps and n.speed_mbps >= 1000 else
                f"{n.speed_mbps} Mb/s" if n.speed_mbps else "unknown")
        head = [Text(n.name, style="bold", no_wrap=True, overflow="ellipsis"),
                Text(f"{'Up' if n.up else 'Down'} · link {link} · MTU {n.mtu or 'n/a'} · "
                     f"{', '.join(n.ipv4) or 'no IPv4 address'}", style="grey62")]
        gw = max(10, w // 2 - 12)
        graph = Text("Down ", style="grey62")
        graph.append(spark(self.hist.get(f"nic.{n.name}.rx"), gw), style="green3")
        graph.append("  Up ", style="grey62")
        graph.append(spark(self.hist.get(f"nic.{n.name}.tx"), gw), style="dark_orange")
        head.append(graph)
        rows = [Row("Throughput (OS)", header=True),
                self.metric("  Download", f"nic.{n.name}.rx", n.rx_bps, "bits", "os"),
                self.metric("  Upload", f"nic.{n.name}.tx", n.tx_bps, "bits", "os"),
                Row("Totals since boot", header=True),
                self.static("  Received", gb(n.rx_bytes), "os"), self.static("  Sent", gb(n.tx_bytes), "os"),
                self.static("  Errors", FORMATS["count"](n.errors), "os"), self.static("  Drops", FORMATS["count"](n.drops), "os")]
        return head, rows

    def page_board(self, _id, w, _f):
        board = self.by_comp.get("board", [])
        names = []
        for s in board:
            top = s.hardware.split(" / ", 1)[0]
            if top not in names:
                names.append(top)
        head = [Text(" · ".join(names) or "Motherboard", style="bold", no_wrap=True, overflow="ellipsis")]
        rails = [s for s in board if s.kind == "voltage"]
        vrm = [s for s in board if s.kind == "temperature" and any(k in s.name.lower() for k in ("vrm", "mos", "vsoc", "vcore"))]
        if not board:
            head.append(Text("No motherboard sensors (Windows: PawnIO + elevated gputaskman for the Super I/O chip; "
                             "Linux: the nct67xx/it87 driver must be loaded)", style="grey50"))
        else:
            head.append(Text(f"{len(rails)} voltage rails · {len(vrm)} VRM temperatures · "
                             f"{len(fans(SensorReading(board)))} spinning fans", style="grey62"))
        return head, self.sensor_rows(board)

    def page_battery(self, _id, _w, _f):
        return [Text("Battery", style="bold")], self.sensor_rows(self.by_comp.get("battery", []))

    def page_sensors(self, _id, _w, filt):
        sensors = [s for s in self.h.sensors.sensors if filt == "all" or s.kind == filt]
        head = [Text(f"All sensors · source: {self.h.sensors.source or 'none'} · filter: {filt} (f)", style="bold")]
        return head, self.sensor_rows(sensors, by_hardware=True)


def health_style(health: str | None) -> str:
    return {"healthy": "green3", "warning": "yellow", "failing": "bold red"}.get(health or "", "grey50")


# ---------------------------------------------------------------------------------------------- the screen

class NavList(OptionList):
    """OptionList whose page keys survive a rebuild: Textual 8's page up/down read a line map that is only refreshed
    on the next render, so pressing them right after the options change raised KeyError (found by the self-test)."""

    def action_page_up(self) -> None:
        try:
            super().action_page_up()
        except (KeyError, IndexError):
            self.action_first()

    def action_page_down(self) -> None:
        try:
            super().action_page_down()
        except (KeyError, IndexError):
            self.action_last()


class HardwareScreen(Screen):
    """Pushed over the GPU view with h; the app feeds it every sample through ``show``."""

    BINDINGS = [
        Binding("escape,h", "app.pop_screen", "GPU view"),
        Binding("f", "cycle_filter", "Filter sensors"),
        Binding("r", "reset_stats", "Reset min/avg/max"),
        Binding("tab", "focus_next", "Switch pane", show=False),
        Binding("p", "app.pause", "Pause"),
        Binding("u", "app.check_update", "Update"),
        Binding("q", "app.quit", "Quit"),
    ]
    DEFAULT_CSS = """
    HardwareScreen { layout: vertical; }
    #hw-body { height: 1fr; }
    #hw-nav { width: 34; min-width: 20; height: 1fr; border: round $accent; }
    #hw-right { width: 1fr; height: 1fr; }
    #hw-head { height: auto; max-height: 50%; border: round $success; padding: 0 1; }
    #hw-table { height: 1fr; min-height: 5; border: round $primary; }
    #hw-notes { height: auto; max-height: 4; color: $text-muted; padding: 0 1; }
    """

    def __init__(self, history: HardwareHistory) -> None:
        super().__init__()
        self.history = history
        self.filter_idx = 0
        self.sample: HardwareSample | None = None
        self.page_id = "summary"
        self._pages: list[tuple[str, str]] = []
        self._rows: list[tuple] = []
        self._signature: tuple = ()
        self.errors: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="hw-body"):
            yield NavList(id="hw-nav")
            with Vertical(id="hw-right"):
                with VerticalScroll(id="hw-head"):
                    yield Static("Sampling hardware...", id="hw-head-body")
                yield DataTable(id="hw-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="hw-notes")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#hw-table", DataTable)
        for col in COLUMNS:
            t.add_column(col, key=col)
        self.query_one("#hw-nav", OptionList).border_title = "Hardware"
        self.query_one("#hw-nav", OptionList).focus()
        if self.sample is not None:
            self.show(self.sample)

    # ------------------------------------------------------------------ actions

    def action_cycle_filter(self) -> None:
        self.filter_idx = (self.filter_idx + 1) % len(SENSOR_FILTERS)
        if self.page_id != "sensors":
            self.page_id = "sensors"
            self._select_nav()
        self.redraw()

    def action_reset_stats(self) -> None:
        self.history.reset()
        reset = getattr(self.app, "reset_hardware_stats", None)
        if reset:
            reset()
        self.notify("Min/avg/max reset; they restart from the next reading.")

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id and event.option.id != self.page_id:
            self.page_id = event.option.id
            self._signature = ()
            self.redraw()

    # ------------------------------------------------------------------ rendering

    def show(self, h: HardwareSample) -> None:
        self.sample = h
        if self.is_mounted:
            self.redraw()

    def redraw(self) -> None:
        """Draw the current page. Never raises: a failure is logged and shown, and the next refresh tries again."""
        if self.sample is None or not self.is_mounted:
            return
        try:
            self._draw(self.sample)
        except Exception as error:  # noqa: BLE001 - the hardware view must never take the app down
            self._fail(error)

    def _fail(self, error: Exception) -> None:
        from . import crashlog

        where = crashlog.write(error, f"hardware view, page {self.page_id}")
        message = f"{type(error).__name__}: {error}"
        self.errors.append(message)
        recorder = getattr(self.app, "render_errors", None)
        if isinstance(recorder, list):
            recorder.append(message)
        try:
            self.query_one("#hw-notes", Static).update(Text(
                f"This page failed to draw ({message}); the details are in {where or 'the crash log'}. "
                "The rest of the app keeps running.", style="bold red"))
        except Exception:  # noqa: BLE001
            pass

    def _snapshots(self):
        return getattr(self.app, "snapshots", None) or []

    def _draw(self, h: HardwareSample) -> None:
        model = Model(h, self._snapshots(), self.history)
        pages = model.pages()
        self._update_nav(pages)
        if self.page_id not in {p.id for p in pages}:
            self.page_id = "summary"
            self._select_nav()
        width = max(30, self.query_one("#hw-right").size.width - 4)
        head, rows = model.build(self.page_id, width, SENSOR_FILTERS[self.filter_idx])
        self.query_one("#hw-head-body", Static).update(Group(*head))
        title = next((p.label for p in pages if p.id == self.page_id), "")
        self.query_one("#hw-head", VerticalScroll).border_title = title
        self.query_one("#hw-table", DataTable).border_title = (
            "Now · Min · Avg · Max this session (r resets)" + (" · f filters" if self.page_id == "sensors" else ""))
        self._fill_table(rows)
        notes = h.notes + h.sensors.notes
        self.query_one("#hw-notes", Static).update(
            Text("  ·  ".join(notes), style="yellow") if notes else
            Text(f"sampled in {h.cost_ms:.1f} ms · sensors: {h.sensors.source or 'none'}", style="grey50"))

    def _update_nav(self, pages: list[Page]) -> None:
        wanted = [(p.section, p.id, p.label) for p in pages]
        if wanted == self._pages:
            return
        nav = self.query_one("#hw-nav", OptionList)
        options = []
        section = None
        for p in pages:
            if p.section != section:
                section = p.section
                options.append(Option(Text(section.upper(), style="bold grey62"), disabled=True))
            options.append(Option(Text(f" {short(p.label)}", no_wrap=True, overflow="ellipsis"), id=p.id))
        nav.clear_options()
        nav.add_options(options)
        self._pages = wanted
        self._select_nav()

    def _select_nav(self) -> None:
        nav = self.query_one("#hw-nav", OptionList)
        try:
            nav.highlighted = nav.get_option_index(self.page_id)
        except Exception:  # noqa: BLE001 - the page is gone; the next draw falls back to the summary
            pass

    def _fill_table(self, rows: list[Row]) -> None:
        t = self.query_one("#hw-table", DataTable)
        cells = []
        for r in rows:
            if r.header:
                cells.append((Text(r.label, style="bold cyan", no_wrap=True, overflow="ellipsis"), "", "", "", "", ""))
            else:
                cells.append((Text(r.label, no_wrap=True, overflow="ellipsis"), Text(r.now, style=r.style),
                              Text(r.min, style="grey62"), Text(r.avg, style="grey62"), Text(r.max, style="grey62"),
                              Text(r.source, style="grey50")))
        signature = (self.page_id, tuple((r.label, r.header) for r in rows))
        if signature == self._signature and len(cells) == t.row_count:
            # Same layout: update only the cells that changed, keeping the cursor and scroll position.
            for index, (new, old) in enumerate(zip(cells, self._rows)):
                for col, (a, b) in enumerate(zip(new, old)):
                    if str(a) != str(b) or (isinstance(a, Text) and isinstance(b, Text) and a.style != b.style):
                        t.update_cell(f"r{index}", COLUMNS[col], a)
            self._rows = cells
            return
        cursor = t.cursor_row
        t.clear()
        for index, row in enumerate(cells):
            t.add_row(*row, key=f"r{index}")  # index keys: unique however sources name their sensors
        if not cells:
            t.add_row(Text("nothing reported here", style="grey50"), "", "", "", "", "", key="empty")
        self._rows, self._signature = cells, signature
        if 0 <= cursor < t.row_count:
            t.move_cursor(row=cursor)

