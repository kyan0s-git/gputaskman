"""The hardware view: CPU, memory, disks, network and sensors, Task Manager and HWiNFO style.

Everything shown here is OS- or sensor-reported machine telemetry (``machine.py``, ``sensors.py``): never GPU bytes
and never added to a GPU total. Unknown values show ``n/a``; a rate's first reading shows ``…``.
"""

from __future__ import annotations

from collections import deque

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from .machine import HardwareSample
from .sensors import Sensor, SensorReading

SPARK = "▁▂▃▄▅▆▇█"
SENSOR_FILTERS = ("all", "temperature", "power", "fan", "voltage", "clock", "load")
CPU_TEMP_NAMES = ("cpu package", "core (tctl/tdie)", "tctl/tdie", "tctl", "tdie", "package id 0", "core max",
                  "cpu", "cpu die (average)", "core average")


def spark(values, width: int, top: float | None = None) -> str:
    vals = list(values)[-width:]
    known = [v for v in vals if v is not None]
    peak = top if top else (max(known) if known else 1.0) or 1.0
    return "".join(SPARK[min(max(int(v / peak * (len(SPARK) - 1) + 0.5), 0), len(SPARK) - 1)] if v is not None else " "
                   for v in vals).rjust(width)


def bar(value: float | None, total: float, width: int, style: str) -> Text:
    t = Text()
    if value is None or not total:
        t.append("·" * width, style="grey27")
        return t
    n = max(0, min(width, int(round(value / total * width))))
    t.append("█" * n, style=style)
    t.append("░" * (width - n), style="grey27")
    return t


def pct_style(v: float | None) -> str:
    return "grey50" if v is None else "red" if v >= 90 else "yellow" if v >= 60 else "green3"


def temp_style(v: float | None) -> str:
    return "grey50" if v is None else "red" if v >= 90 else "dark_orange" if v >= 75 else "yellow" if v >= 60 else "green3"


def gb(n: float | None) -> str:
    return "n/a" if n is None else f"{n / 2**30:.1f} GB"


def rate(bps: float | None) -> str:
    if bps is None:
        return "…"
    for unit, scale in (("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3)):
        if bps >= scale:
            return f"{bps / scale:.1f} {unit}"
    return f"{bps:.0f} B/s"


def bits(bps: float | None) -> str:
    """Network rates in bits per second, as adapters are rated."""
    if bps is None:
        return "…"
    b = bps * 8
    for unit, scale in (("Gb/s", 1e9), ("Mb/s", 1e6), ("Kb/s", 1e3)):
        if b >= scale:
            return f"{b / scale:.1f} {unit}"
    return f"{b:.0f} b/s"


def uptime(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------------------------- sensor picks

def cpu_temperature(reading: SensorReading) -> Sensor | None:
    """The CPU's headline temperature: package/Tctl first, then the hottest CPU temperature."""
    temps = [s for s in reading.sensors if s.kind == "temperature" and s.value is not None and
             (s.group in ("Cpu", "k10temp", "coretemp", "zenpower", "cpu_thermal") or s.hardware.startswith("CPU"))]
    for wanted in CPU_TEMP_NAMES:
        for s in temps:
            if s.name.lower() == wanted:
                return s
    return max(temps, key=lambda s: s.value, default=None)


def package_power(reading: SensorReading) -> Sensor | None:
    power = [s for s in reading.sensors if s.kind == "power" and s.value is not None and s.group == "Cpu"]
    for s in power:
        if s.name.lower() in ("package", "cpu package", "package power"):
            return s
    return power[0] if power else None


def fans(reading: SensorReading) -> list[Sensor]:
    """Fans that are turning: a stopped fan (0 RPM, often an empty header) is a reading, not a spinning fan."""
    return [s for s in reading.sensors if s.kind == "fan" and s.value is not None and s.value > 0]


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


# ---------------------------------------------------------------------------------------------- the screen

class HardwareHistory:
    """60-sample histories for the sparklines, kept by the app so they survive leaving and reopening the view."""

    def __init__(self, n: int = 60) -> None:
        self.cpu: deque = deque(maxlen=n)
        self.ram: deque = deque(maxlen=n)
        self.disk_read: deque = deque(maxlen=n)
        self.disk_write: deque = deque(maxlen=n)
        self.net_rx: deque = deque(maxlen=n)
        self.net_tx: deque = deque(maxlen=n)
        self.temp: deque = deque(maxlen=n)
        self.power: deque = deque(maxlen=n)

    def add(self, h: HardwareSample) -> None:
        self.cpu.append(h.cpu.util_pct)
        self.ram.append(100 * h.memory.used / h.memory.total if h.memory.total else None)

        def total(values):
            known = [v for v in values if v is not None]
            return sum(known) if known else None

        self.disk_read.append(total(d.read_bps for d in h.disks))
        self.disk_write.append(total(d.write_bps for d in h.disks))
        self.net_rx.append(total(n.rx_bps for n in h.nics))
        self.net_tx.append(total(n.tx_bps for n in h.nics))
        temp, power = cpu_temperature(h.sensors), package_power(h.sensors)
        self.temp.append(temp.value if temp else None)
        self.power.append(power.value if power else None)


class HardwareScreen(Screen):
    """Pushed over the GPU view with h; the app feeds it every sample through ``show``."""

    BINDINGS = [
        Binding("escape,h", "app.pop_screen", "GPU view"),
        Binding("f", "cycle_filter", "Sensor filter"),
        Binding("p", "app.pause", "Pause"),
        Binding("u", "app.check_update", "Update"),
        Binding("q", "app.quit", "Quit"),
    ]
    DEFAULT_CSS = """
    HardwareScreen { layout: vertical; }
    #hw-top { height: auto; max-height: 60%; }
    #hw-row1, #hw-row2 { height: auto; }
    #hw-cpu, #hw-mem, #hw-disk, #hw-net { width: 1fr; height: auto; padding: 0 1; }
    #hw-cpu { border: round $success; }
    #hw-mem { border: round $accent; }
    #hw-disk { border: round $warning; }
    #hw-net { border: round $secondary; }
    #hw-sensors { height: 1fr; min-height: 6; border: round $primary; }
    #hw-notes { height: auto; color: $text-muted; padding: 0 1; }
    """

    def __init__(self, history: HardwareHistory) -> None:
        super().__init__()
        self.history = history
        self.filter_idx = 0
        self.sample: HardwareSample | None = None
        self._rows: list[str] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="hw-top"):
            with Horizontal(id="hw-row1"):
                yield Static("Sampling CPU...", id="hw-cpu")
                yield Static("Sampling memory...", id="hw-mem")
            with Horizontal(id="hw-row2"):
                yield Static("Sampling disks...", id="hw-disk")
                yield Static("Sampling network...", id="hw-net")
        table = DataTable(id="hw-sensors", cursor_type="row", zebra_stripes=True)
        yield table
        yield Static("", id="hw-notes")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#hw-sensors", DataTable)
        for col in ("Sensor", "Current", "Min", "Max", "Source"):
            t.add_column(col, key=col)
        t.border_title = "Sensors"
        self.query_one("#hw-cpu", Static).border_title = "CPU"
        self.query_one("#hw-mem", Static).border_title = "Memory"
        self.query_one("#hw-disk", Static).border_title = "Disks"
        self.query_one("#hw-net", Static).border_title = "Network"
        if self.sample is not None:
            self.show(self.sample)

    def action_cycle_filter(self) -> None:
        self.filter_idx = (self.filter_idx + 1) % len(SENSOR_FILTERS)
        if self.sample is not None:
            self.render_sensors(self.sample)

    # ------------------------------------------------------------------ rendering

    @property
    def half(self) -> int:
        return max(30, self.size.width // 2 - 6)

    def show(self, h: HardwareSample) -> None:
        self.sample = h
        if not self.is_mounted:
            return
        self.query_one("#hw-cpu", Static).update(self.render_cpu(h))
        self.query_one("#hw-mem", Static).update(self.render_memory(h))
        self.query_one("#hw-disk", Static).update(self.render_disks(h))
        self.query_one("#hw-net", Static).update(self.render_network(h))
        self.render_sensors(h)
        notes = h.notes + h.sensors.notes
        self.query_one("#hw-notes", Static).update(
            Text("  ·  ".join(notes), style="yellow") if notes else Text(f"sampled in {h.cost_ms:.1f} ms", style="grey50"))

    def render_cpu(self, h: HardwareSample) -> Group:
        c = h.cpu
        w = self.half
        lines = [Text(c.model, style="bold", no_wrap=True, overflow="ellipsis")]
        util = Text("Util  ")
        util.append_text(bar(c.util_pct, 100, max(8, w - 16), pct_style(c.util_pct)))
        util.append(f" {c.util_pct:5.1f}%" if c.util_pct is not None else "     …", style="bold")
        lines.append(util)
        graph = Text("      ")
        graph.append(spark(self.history.cpu, max(8, w - 16), 100), style="green3")
        graph.append(" 60s", style="grey50")
        lines.append(graph)
        # Per-thread load as a compact grid of small bars, Task Manager's "logical processors" view.
        cols = max(1, min(16, (w - 6) // 6))
        for start in range(0, len(c.per_core_pct), cols):
            row = Text("      ")
            for v in c.per_core_pct[start:start + cols]:
                row.append(SPARK[min(int((v or 0) / 100 * (len(SPARK) - 1) + 0.5), len(SPARK) - 1)] if v is not None else "·",
                           style=pct_style(v))
                row.append(f"{v:3.0f} " if v is not None else "  … ", style="grey62")
                row.append(" ")
            lines.append(row)
        info = Text(no_wrap=True, overflow="ellipsis")
        info.append("Speed ", style="grey62")
        info.append(f"{c.speed_mhz / 1000:.2f} GHz" if c.speed_mhz else "n/a", style="bold")
        if c.base_mhz:
            info.append(f"  base {c.base_mhz / 1000:.2f} GHz", style="grey62")
        if c.max_mhz:
            info.append(f"  max {c.max_mhz / 1000:.2f} GHz", style="grey62")
        lines.append(info)
        temp, power = cpu_temperature(h.sensors), package_power(h.sensors)
        therm = Text(no_wrap=True, overflow="ellipsis")
        therm.append("Temp  ", style="grey62")
        therm.append(f"{temp.value:.0f}°C" if temp else "n/a", style=temp_style(temp.value if temp else None))
        if temp is not None:
            therm.append(f" ({temp.name})", style="grey50")
        therm.append("   Package ", style="grey62")
        therm.append(f"{power.value:.1f} W" if power else "n/a", style="bold yellow" if power else "grey50")
        lines.append(therm)
        if temp is not None or power is not None:
            trend = Text("      ")
            trend.append(spark(self.history.temp, max(8, w // 2 - 8), 110), style="red")
            trend.append(" °C ", style="grey50")
            trend.append(spark(self.history.power, max(8, w // 2 - 8)), style="yellow")
            trend.append(" W", style="grey50")
            lines.append(trend)
        counts = Text(no_wrap=True, overflow="ellipsis", style="grey62")
        counts.append(f"{c.logical} threads" + (f" / {c.physical} cores" if c.physical else ""))
        if c.processes is not None:
            counts.append(f"  ·  {c.processes} processes")
        if c.threads is not None:
            counts.append(f"  ·  {c.threads:,} threads")
        if c.handles is not None:
            counts.append(f"  ·  {c.handles:,} handles")
        lines.append(counts)
        tail = Text(no_wrap=True, overflow="ellipsis", style="grey62")
        tail.append(f"Up {uptime(c.uptime_s)}")
        if c.load_avg:
            tail.append(f"  ·  load {c.load_avg[0]:.2f} {c.load_avg[1]:.2f} {c.load_avg[2]:.2f}")
        if c.speed_note:
            tail.append(f"  ·  {c.speed_note}", style="yellow")
        lines.append(tail)
        return Group(*lines)

    def render_memory(self, h: HardwareSample) -> Group:
        m = h.memory
        w = self.half
        width = max(8, w - 26)
        used = Text("In use ")
        used.append_text(bar(m.used, m.total, width, "dodger_blue2"))
        used.append(f" {gb(m.used)} / {gb(m.total)}", style="bold")
        lines = [used]
        graph = Text("       ")
        graph.append(spark(self.history.ram, width, 100), style="dodger_blue2")
        graph.append(" 60s", style="grey50")
        lines.append(graph)
        detail = Table.grid(padding=(0, 2))
        detail.add_column(style="grey62")
        detail.add_column(style="bold")
        detail.add_row("Available", gb(m.available))
        detail.add_row("Cached", gb(m.cached))
        if m.committed is not None:
            detail.add_row("Committed", f"{gb(m.committed)} / {gb(m.commit_limit)}")
        if m.swap_total:
            detail.add_row("Swap / pagefile", f"{gb(m.swap_used)} / {gb(m.swap_total)}")
        lines.append(detail)
        return Group(*lines)

    def render_disks(self, h: HardwareSample) -> Group:
        w = self.half
        table = Table.grid(padding=(0, 1))
        for col in ("Disk", "Read", "Write", "Active"):
            table.add_column(col)
        table.add_row(*(Text(x, style="grey62") for x in ("Disk", "Read", "Write", "Active")))
        bw = max(6, w - 50)
        for d in h.disks:
            active = Text()
            active.append_text(bar(d.active_pct, 100, bw, pct_style(d.active_pct)))
            active.append(f" {d.active_pct:3.0f}%" if d.active_pct is not None else "  n/a", style="grey62")
            table.add_row(Text(d.name, no_wrap=True, overflow="ellipsis"), rate(d.read_bps), rate(d.write_bps), active)
        lines: list = [table]
        io = Text("      ")
        io.append(spark(self.history.disk_read, max(8, w // 2 - 10)), style="cyan")
        io.append(" read ", style="grey50")
        io.append(spark(self.history.disk_write, max(8, w // 2 - 10)), style="magenta")
        io.append(" write", style="grey50")
        lines.append(io)
        vols = Table.grid(padding=(0, 1))
        for _ in range(3):
            vols.add_column()
        vw = max(6, w - 40)
        for v in h.volumes[:8]:
            used = Text()
            used.append_text(bar(v.used, v.total, vw, "red" if v.total and v.free / v.total < 0.1 else "dark_cyan"))
            vols.add_row(Text(v.mount, no_wrap=True, overflow="ellipsis"), used,
                         Text(f"{gb(v.free)} free of {gb(v.total)}", style="grey62"))
        if h.volumes:
            lines.append(vols)
        return Group(*lines)

    def render_network(self, h: HardwareSample) -> Group:
        w = self.half
        table = Table.grid(padding=(0, 1))
        for _ in range(4):
            table.add_column()
        table.add_row(*(Text(x, style="grey62") for x in ("Adapter", "Down", "Up", "Link / address")))
        shown = [n for n in h.nics if n.up] or h.nics
        for n in shown[:8]:
            link = f"{n.speed_mbps / 1000:g} Gb/s" if n.speed_mbps and n.speed_mbps >= 1000 else (
                f"{n.speed_mbps} Mb/s" if n.speed_mbps else "")
            addr = ", ".join(n.ipv4[:2])
            table.add_row(Text(n.name, no_wrap=True, overflow="ellipsis", style="bold" if n.up else "grey50"),
                          bits(n.rx_bps), bits(n.tx_bps),
                          Text(" ".join(x for x in (link, addr) if x) or ("down" if not n.up else ""),
                               style="grey62", no_wrap=True, overflow="ellipsis"))
        traffic = Text("      ")
        traffic.append(spark(self.history.net_rx, max(8, w // 2 - 10)), style="green3")
        traffic.append(" down ", style="grey50")
        traffic.append(spark(self.history.net_tx, max(8, w // 2 - 10)), style="dark_orange")
        traffic.append(" up", style="grey50")
        hidden = len(h.nics) - len(shown)
        extra = [Text(f"{hidden} adapter(s) down or disconnected", style="grey50")] if hidden > 0 else []
        return Group(table, traffic, *extra)

    def render_sensors(self, h: HardwareSample) -> None:
        t = self.query_one("#hw-sensors", DataTable)
        wanted = SENSOR_FILTERS[self.filter_idx]
        source = h.sensors.source or "none"
        t.border_title = f"Sensors · {source} · filter: {wanted} (f)"
        sensors = [s for s in h.sensors.sensors if wanted == "all" or s.kind == wanted]
        keys = []
        rows = []
        last_hw = None
        for s in sensors:
            if s.hardware != last_hw:
                last_hw = s.hardware
                key = f"hw:{s.hardware}"
                keys.append(key)
                rows.append((key, (Text(s.hardware, style="bold cyan"), "", "", "", "")))
            style = temp_style(s.value) if s.kind == "temperature" else ""
            rows.append((s.key, (Text(f"  {s.name}", no_wrap=True, overflow="ellipsis"), Text(s.text(), style=style),
                                 Text(s.text(s.min) if s.min is not None else "", style="grey62"),
                                 Text(s.text(s.max) if s.max is not None else "", style="grey62"),
                                 Text(s.source, style="grey50"))))
            keys.append(s.key)
        if keys == self._rows:  # same layout: update cells in place so the cursor and scroll position stay put
            for key, cells in rows:
                for col, cell in zip(("Sensor", "Current", "Min", "Max", "Source"), cells):
                    t.update_cell(key, col, cell)
            return
        cursor = t.cursor_row
        t.clear()
        for key, cells in rows:
            t.add_row(*cells, key=key)
        if not rows:
            t.add_row(Text("no sensors" + (f" of kind {wanted}" if wanted != "all" else ""), style="grey50"),
                      "", "", "", "", key="empty")
        self._rows = keys
        if cursor < t.row_count:
            t.move_cursor(row=cursor)
