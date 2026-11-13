"""``gputaskman selftest``: stress the TUI so a crash shows up here, with its traceback, instead of on screen.

Runs the real app headless (no window, no terminal needed) and keeps it busy: it opens every hardware page,
presses every key, resizes from tiny to huge, toggles screens, and feeds it hostile readings (missing values, NaN,
infinities, negatives, huge numbers, odd and duplicate names, hundreds of cores and sensors). Two phases:

* **real**: the app samples this machine as usual (GPU collectors, CPU, disks, network, sensors).
* **fuzz**: randomized readings from a seeded generator, so a failure can be replayed with ``--seed``.

Any unhandled exception, or any error the hardware view caught and survived, fails the run. Nothing is sent
anywhere; tracebacks go to the terminal and the local crash log.
"""

from __future__ import annotations

import asyncio
import math
import random
import sys
import time
from unittest import mock

from .machine import CpuInfo, DiskIO, HardwareSample, MemoryInfo, Nic, Volume
from .sensors import KIND_ORDER, LHM_COMPONENT, DiskHealth, Sensor, SensorReading

WEIRD_NAMES = ("", " ", "[bold red]markup[/]", "[", "]", "<script>", "名前", "🔥 hot", "a" * 300, "line\nbreak",
               "tab\there", "C:\\", "/dev/nvme0n1", "Core #1", "Core #1", "Samsung SSD 990 PRO 2TB", "\x00nul", "%s%d{}")
SIZES = ((10, 5), (20, 8), (40, 12), (80, 24), (120, 40), (170, 52), (250, 70), (33, 100), (300, 20))
NAV_KEYS = ("down", "down", "down", "up", "pagedown", "pageup", "end", "home", "tab", "tab", "down", "enter")


def wild(rng: random.Random, typical: float, spread: float = 2.0):
    """Mostly plausible values, sometimes hostile ones."""
    roll = rng.random()
    if roll < 0.12:
        return None
    if roll < 0.15:
        return rng.choice((math.nan, math.inf, -math.inf))
    if roll < 0.19:
        return rng.choice((-typical, 0.0, typical * 1e9, -1.0, 1e300, 5e-324))
    return abs(rng.gauss(typical, typical * spread / 3))


def name(rng: random.Random, plain: str) -> str:
    return rng.choice(WEIRD_NAMES) if rng.random() < 0.15 else plain


def fuzz_sample(rng: random.Random) -> HardwareSample:
    logical = rng.choice((0, 1, 2, 4, 8, 16, 32, 64, 128, 256))
    per_core = [wild(rng, 40) for _ in range(logical)]
    per_mhz = [wild(rng, 3500) for _ in range(logical)] if rng.random() < 0.7 else []
    cpu = CpuInfo(name(rng, "AMD Ryzen 9 7950X"), logical, rng.choice((None, max(1, logical // 2))), wild(rng, 40),
                  per_core, per_mhz, wild(rng, 4000), wild(rng, 3000), wild(rng, 5000),
                  rng.choice((None, (0.5, 1.0, 2.0), (math.nan, -1.0, 1e9))), rng.choice((None, 300, -1)),
                  rng.choice((None, 4000)), rng.choice((None, 120000)), wild(rng, 86400 * 3),
                  rng.choice(("", "speed note")))
    total = rng.choice((0, 1, 16 * 2**30, 2**50))
    memory = MemoryInfo(total, rng.randint(0, max(0, total)), rng.randint(0, max(0, total)), rng.choice((None, 2**30)),
                        rng.choice((None, 2**33)), rng.choice((None, 0, 2**34)), rng.choice((0, 2**32)), rng.choice((0, 2**31)))
    disks = [DiskIO(name(rng, f"PhysicalDrive{i}"), rng.choice((None, "Samsung SSD 990 PRO 2TB", "")), wild(rng, 1e8),
                    wild(rng, 1e8), rng.randint(0, 2**50), rng.randint(0, 2**50), wild(rng, 30))
             for i in range(rng.choice((0, 1, 2, 5, 20)))]
    volumes = [Volume(name(rng, f"{chr(67 + i)}:\\"), name(rng, f"\\\\?\\Volume{i}"), rng.choice(("NTFS", "ext4", "")),
                      t, rng.randint(0, max(0, t)), rng.randint(0, max(0, t))) for i, t in
               enumerate(rng.choice((0, 2**40)) for _ in range(rng.choice((0, 1, 3, 30))))]
    nics = [Nic(name(rng, rng.choice(("Ethernet", "Wi-Fi", "vEthernet (WSL)"))), rng.random() < 0.7,
                rng.choice((None, 0, 1000, 2500, 100000)), rng.choice((None, 1500)), wild(rng, 1e6), wild(rng, 1e5),
                rng.randint(0, 2**50), rng.randint(0, 2**50), rng.randint(0, 99), rng.randint(0, 99),
                rng.choice(([], ["192.168.1.2"], ["10.0.0.1", "fe80::1"]))) for _ in range(rng.choice((0, 1, 3, 12)))]
    groups = list(LHM_COMPONENT) + ["k10temp", "coretemp", "nct6799", "amdgpu", "nvme", "jc42", "acpitz", "weird"]
    sensors = []
    for i in range(rng.choice((0, 5, 40, 400))):
        kind = rng.choice(KIND_ORDER + ("other",))
        hw = name(rng, rng.choice(("AMD Ryzen 9 7950X", "Nuvoton NCT6799D", "NVIDIA GeForce RTX 4080 SUPER",
                                   "Samsung SSD 990 PRO 2TB", "G.Skill F5-6000", "Battery")))
        sensors.append(Sensor(rng.choice((f"/s/{i}", "/dup", "")), hw, rng.choice(groups),
                              name(rng, rng.choice(("Core (Tctl/Tdie)", "Package", "Core #1 VID", "CPU Fan", "+12V",
                                                    "VRM MOS", "GPU Hot Spot", "Core #1 Distance to TjMax", "Core #1"))),
                              kind, wild(rng, 50), rng.choice(("°C", "W", "V", "RPM", "MHz", "%", "", "B/s", "GB")),
                              rng.choice(("lhm", "hwmon", "rapl", "battery")), wild(rng, 40), wild(rng, 60), wild(rng, 50),
                              wild(rng, 100)))
    health = [DiskHealth(rng.choice((None, "0", "1", "nvme0", "sda")), name(rng, "Samsung SSD 990 PRO 2TB"),
                         health=rng.choice((None, "healthy", "warning", "failing", "???")), temperature=wild(rng, 40),
                         power_on_hours=wild(rng, 5000), wear_pct=wild(rng, 5), read_errors=rng.choice((None, 0, -1, 10**12)),
                         source=rng.choice(("wmi", "smartctl"))) for _ in range(rng.choice((0, 1, 3)))]
    notes = [name(rng, "note") for _ in range(rng.choice((0, 1, 5)))]
    reading = SensorReading(sensors, notes, rng.choice(("", "LibreHardwareMonitor (helper)", "hwmon + RAPL")), health)
    return HardwareSample(time.time(), cpu, memory, disks, volumes, nics, reading, list(notes), wild(rng, 2) or 0.0)


def fuzz_snapshots(rng: random.Random) -> list:
    """GPU snapshots with plausible-range telemetry (the GPU view's own robustness is covered by its own tests)."""
    from .collector import Snapshot
    from .telemetry import DeviceTelemetry

    out = []
    for i in range(rng.choice((0, 1, 2))):
        maybe = lambda v: v if rng.random() < 0.8 else None  # noqa: E731
        nv = DeviceTelemetry(index=i, name=name(rng, "NVIDIA GeForce RTX 4080 SUPER"), util_gpu=maybe(rng.uniform(0, 100)),
                             util_mem=maybe(rng.uniform(0, 100)), temp=maybe(rng.uniform(20, 95)),
                             power_w=maybe(rng.uniform(5, 450)), power_limit_w=maybe(320.0),
                             sm_clock=maybe(rng.randint(200, 3000)), mem_clock=maybe(rng.randint(400, 12000)),
                             fan=maybe(rng.randint(0, 100)), enc=maybe(0.0), dec=maybe(5.0), pcie_gen=maybe(4),
                             pcie_width=maybe(16), throttle=rng.choice(([], None, ["power cap"])))
        total = rng.choice((0, 16 * 2**30))
        out.append(Snapshot(taken=time.time(), adapter=None, adapters=[], nv=nv, driver=rng.choice(("", "566.03")), cuda="",
                            vram_total=total, vram_used=rng.choice((None, total // 3)), shared_used=rng.choice((None, 2**28))))
    return out


class Report:
    def __init__(self) -> None:
        self.actions = 0
        self.samples = 0
        self.pages: set[str] = set()
        self.failures: list[str] = []


async def exercise(app, pilot, rng: random.Random, seconds: float, report: Report, fuzz: bool) -> None:
    """Keep the app busy for ``seconds``: navigation, keys, resizes, screen toggles, and (fuzz) hostile samples."""
    from .hwview import HardwareScreen

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        roll = rng.random()
        if fuzz and roll < 0.35:
            sample, snaps = fuzz_sample(rng), fuzz_snapshots(rng)
            app.snapshots = snaps if rng.random() < 0.5 else app.snapshots
            app.hw_history.add(sample, app.snapshots)
            app.hw_sample = sample
            for screen in app.screen_stack:
                if isinstance(screen, HardwareScreen):
                    screen.show(sample)
            report.samples += 1
        elif roll < 0.55:
            await pilot.press(rng.choice(NAV_KEYS))
        elif roll < 0.63:
            await pilot.resize_terminal(*rng.choice(SIZES))
        elif roll < 0.72:
            if not isinstance(app.screen, HardwareScreen):
                await pilot.press("h")
            else:
                await pilot.press(rng.choice(("escape", "f", "r", "f")))
        elif roll < 0.76:
            await pilot.press(rng.choice(("m", "c", "a", "s", "question_mark", "escape")))
        else:
            await pilot.pause(0.02)
        report.actions += 1
        screen = app.screen
        if isinstance(screen, HardwareScreen):
            report.pages.add(screen.page_id)
            if rng.random() < 0.2:  # walk every page now and then
                for option in list(screen.query_one("#hw-nav").options):
                    if option.id:
                        screen.page_id = option.id
                        screen.redraw()
                        report.pages.add(option.id)
        if app.render_errors:
            report.failures.extend(app.render_errors)
            app.render_errors.clear()
            break
    await pilot.pause(0.1)


def run(seconds: float = 30.0, seed: int | None = None, real: bool = True, fuzz: bool = True,
        out=sys.stdout) -> int:
    """The self-test. Returns 0 when nothing failed, 1 otherwise."""
    from . import crashlog
    from .app import GpuTaskManager

    seed = random.randrange(1 << 30) if seed is None else seed
    rng = random.Random(seed)
    report = Report()
    phases = [p for p, on in (("real", real), ("fuzz", fuzz)) if on]
    print(f"gputaskman selftest: {seconds:g} s, phases {', '.join(phases)}, seed {seed}", file=out)

    async def phase(kind: str, span: float) -> None:
        app = GpuTaskManager(interval=0.25 if kind == "real" else 5.0)
        patches = [] if kind == "real" else [mock.patch.object(GpuTaskManager, "poll", lambda self: None)]
        for p in patches:
            p.start()
        try:
            async with app.run_test(headless=True, size=(160, 50)) as pilot:
                await pilot.pause(1.0 if kind == "real" else 0.1)
                await exercise(app, pilot, rng, span, report, fuzz=kind == "fuzz")
                await pilot.press("escape")
                await pilot.pause(0.1)
        except Exception as error:  # noqa: BLE001 - this is what the self-test is for
            where = crashlog.write(error, f"selftest {kind} phase, seed {seed}")
            import traceback

            report.failures.append(f"{kind}: {type(error).__name__}: {error} (traceback in {where})")
            traceback.print_exception(error, file=out)
        finally:
            for p in patches:
                p.stop()
        if app.render_errors:
            report.failures.extend(f"{kind}: {e}" for e in app.render_errors)

    for kind in phases:
        if report.failures:
            break
        asyncio.run(phase(kind, seconds / len(phases)))
        print(f"  {kind}: done", file=out)
    print(f"  {report.actions} actions, {report.samples} fuzzed samples, {len(report.pages)} distinct pages", file=out)
    if report.failures:
        print(f"FAILED (replay with --seed {seed}):", file=out)
        for f in report.failures[:20]:
            print(f"  - {f}", file=out)
        print(f"Tracebacks are in {crashlog.path()}", file=out)
        return 1
    print("PASSED: no crash and no drawing error", file=out)
    return 0


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="gputaskman selftest", description="Stress the TUI headless on this machine's "
                                 "real readings and on hostile randomized ones; exits 1 on any crash or drawing error.")
    ap.add_argument("--seconds", type=float, default=30.0, help="total run time (default 30)")
    ap.add_argument("--seed", type=int, help="replay a run (printed at the start of every run)")
    ap.add_argument("--real-only", action="store_true", help="only this machine's real readings")
    ap.add_argument("--fuzz-only", action="store_true", help="only randomized readings")
    args = ap.parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        ap.error("--seconds must be a finite number greater than zero")
    return run(args.seconds, args.seed, real=not args.fuzz_only, fuzz=not args.real_only)
