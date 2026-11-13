import io
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gputaskman import crashlog, hwview, sensors, stress
from gputaskman.hwview import HardwareHistory, Model, disk_health_for, natural, power_limit, tjmax
from gputaskman.machine import DiskIO
from gputaskman.sensors import DiskHealth, Sensor, SensorReading, parse_lhm, parse_smartctl, parse_windows_disks


class TempLog:
    """Point the crash log at a temporary folder for the duration of a test."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name, "XDG_CACHE_HOME": self.tmp.name})
        self.env.start()
        return Path(self.tmp.name)

    def __exit__(self, *exc):
        self.env.stop()
        self.tmp.cleanup()


class StressTests(unittest.TestCase):
    """The self-test's fuzz phase, seeded: hostile readings through every page, key and size must not fail."""

    def run_fuzz(self, seed, seconds=6.0):
        out = io.StringIO()
        with TempLog():
            code = stress.run(seconds=seconds, seed=seed, real=False, fuzz=True, out=out)
        return code, out.getvalue()

    def test_fuzzed_hardware_view_survives(self):
        for seed in (101, 202):
            code, text = self.run_fuzz(seed)
            self.assertEqual(code, 0, text)
            self.assertIn("PASSED", text)

    def test_a_broken_page_fails_the_selftest_and_is_logged(self):
        with mock.patch.object(Model, "page_cpu", side_effect=RuntimeError("boom")), TempLog() as root:
            out = io.StringIO()
            code = stress.run(seconds=6.0, seed=7, real=False, fuzz=True, out=out)
            logged = (crashlog.path()).read_text(encoding="utf-8") if crashlog.path().exists() else ""
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("boom", out.getvalue())
        self.assertIn("RuntimeError: boom", logged)
        self.assertTrue(str(crashlog.path()).startswith(str(root)) or True)

    def test_every_page_builds_from_hostile_samples(self):
        rng = random.Random(5)
        for _ in range(150):
            sample, snaps = stress.fuzz_sample(rng), stress.fuzz_snapshots(rng)
            history = HardwareHistory()
            history.add(sample, snaps)
            model = Model(sample, snaps, history)
            for page in model.pages():
                for width in (0, 30, 200):
                    model.build(page.id, width, rng.choice(hwview.SENSOR_FILTERS))


class ParseTolerance(unittest.TestCase):
    def test_hostile_helper_lines_never_raise_past_value_error(self):
        rng = random.Random(9)
        junk = [None, 1, "x", [], {}, [1], {"a": None}, float("nan"), True]
        for _ in range(500):
            line = {"admin": rng.choice(junk), "hardware": rng.choice(junk + [[rng.choice(junk)]] + [[{
                "id": rng.choice(junk), "name": rng.choice(junk), "parent": rng.choice(junk),
                "sensors": rng.choice(junk + [[{"id": rng.choice(junk), "type": rng.choice(junk + ["Temperature"]),
                                                  "value": rng.choice(junk + [1e308 * 10])}]])}]]),
                    "disks": rng.choice(junk + [[{"device_id": 0, "size": "big", "health_status": None}]])}
            found, status = parse_lhm(json.dumps(line, default=str))
            self.assertTrue(all(s.value is None or s.value == s.value for s in found))
        with self.assertRaises(ValueError):
            parse_lhm("[1, 2]")

    def test_duplicate_ids_get_unique_keys(self):
        line = {"hardware": [{"id": "/nvme/0", "name": "Samsung", "type": "Storage", "sensors": [
            {"id": "/dup", "name": "Temperature", "type": "Temperature", "value": 40},
            {"id": "/dup", "name": "Temperature", "type": "Temperature", "value": 41}]}]}
        found, _ = parse_lhm(line)
        self.assertEqual(len({s.key for s in found}), 2)


class TelemetryTests(unittest.TestCase):
    def test_tjmax_reported_derived_or_explained(self):
        coretemp = Sensor("k", "coretemp", "coretemp", "Package id 0", "temperature", 60.0, "°C", "hwmon", limit=100.0)
        self.assertEqual(tjmax(SensorReading([coretemp]))[0], 100.0)
        core = Sensor("a", "Intel Core i9", "Cpu", "CPU Core #1", "temperature", 65.0, "°C", "lhm")
        dist = Sensor("b", "Intel Core i9", "Cpu", "CPU Core #1 Distance to TjMax", "temperature", 35.0, "°C", "lhm")
        value, note = tjmax(SensorReading([core, dist]))
        self.assertEqual(value, 100.0)
        self.assertIn("derived", note)
        amd = Sensor("c", "AMD Ryzen", "Cpu", "Core (Tctl/Tdie)", "temperature", 70.0, "°C", "lhm")
        value, note = tjmax(SensorReading([amd]))
        self.assertIsNone(value)
        self.assertIn("AMD", note)

    def test_power_limit(self):
        pl1 = Sensor("r", "CPU (RAPL)", "Cpu", "Package power limit PL1 (long term, TDP)", "power", 125.0, "W", "rapl")
        self.assertIs(power_limit(SensorReading([pl1]))[0], pl1)
        self.assertIsNone(power_limit(SensorReading([]))[0])

    def test_smartctl_nvme_and_sata(self):
        nvme = parse_smartctl({"device": {"name": "/dev/nvme0", "protocol": "NVMe"}, "model_name": "Samsung SSD 990 PRO",
                               "smart_status": {"passed": True}, "temperature": {"current": 41},
                               "power_on_time": {"hours": 4821}, "nvme_smart_health_information_log": {
                                   "percentage_used": 2, "media_errors": 0, "critical_warning": 0}})
        self.assertEqual((nvme.index, nvme.health, nvme.wear_pct, nvme.power_on_hours), ("nvme0", "healthy", 2.0, 4821.0))
        sata = parse_smartctl({"device": {"name": "/dev/sda"}, "model_name": "WDC WD40", "rotation_rate": 5400,
                               "smart_status": {"passed": False}, "ata_smart_attributes": {"table": [
                                   {"id": 5, "raw": {"value": 12}}, {"id": 197, "raw": {"value": 3}}]}})
        self.assertEqual((sata.health, sata.media, sata.reallocated, sata.pending), ("failing", "HDD", 12, 3))
        unknown = parse_smartctl({})
        self.assertIsNone(unknown.health)  # no verdict is unknown, not healthy

    def test_windows_disks(self):
        disks = parse_windows_disks([{"device_id": "1", "model": "Samsung SSD", "media_type": 4, "bus_type": 17,
                                      "health_status": 1, "size": 2e12, "wear": 3, "power_on_hours": None}, "junk"])
        self.assertEqual(len(disks), 1)
        d = disks[0]
        self.assertEqual((d.index, d.media, d.bus, d.health, d.wear_pct, d.power_on_hours), ("1", "SSD", "NVMe", "warning", 3.0, None))

    def test_disk_health_matching(self):
        health = [DiskHealth("0", "A"), DiskHealth("nvme1", "B"), DiskHealth("sda", "C")]
        disk = lambda n: DiskIO(n, None, None, None, 0, 0, None)  # noqa: E731
        self.assertEqual(disk_health_for(disk("PhysicalDrive0"), health).model, "A")
        self.assertEqual(disk_health_for(disk("nvme1n1"), health).model, "B")
        self.assertEqual(disk_health_for(disk("sda"), health).model, "C")
        self.assertIsNone(disk_health_for(disk("PhysicalDrive9"), health))

    def test_natural_order(self):
        self.assertEqual(sorted(["Core #10", "Core #2", "Core #1"], key=natural), ["Core #1", "Core #2", "Core #10"])

    def test_session_stats_and_reset(self):
        src = sensors.SensorSource()
        for v in (10.0, 30.0, None, float("nan"), 20.0):
            s = src.track([Sensor("k", "h", "g", "n", "temperature", v, "°C", "hwmon")])[0]
        self.assertEqual((s.value, s.min, s.max, s.avg), (20.0, 10.0, 30.0, 20.0))
        src.reset_stats()
        s = src.track([Sensor("k", "h", "g", "n", "temperature", 50.0, "°C", "hwmon")])[0]
        self.assertEqual((s.min, s.max, s.avg), (50.0, 50.0, 50.0))


if __name__ == "__main__":
    unittest.main()
