import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gputaskman import machine, sensors
from gputaskman.hwview import cpu_temperature, fans, package_power, sensor_summary
from gputaskman.sensors import LinuxSensors, Sensor, SensorReading, lhm_notes, parse_lhm


def write(root: Path, files: dict) -> None:
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{value}\n")


class LinuxSensorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hwmon, self.rapl, self.supply = self.root / "hwmon", self.root / "powercap", self.root / "power_supply"
        self.hwmon.mkdir()
        self.rapl.mkdir()
        self.supply.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def source(self):
        return LinuxSensors(self.hwmon, self.rapl, self.supply)

    def test_hwmon_units_labels_and_unknown(self):
        write(self.hwmon / "hwmon0", {"name": "k10temp", "temp1_input": 61250, "temp1_label": "Tctl"})
        write(self.hwmon / "hwmon1", {"name": "nct6799", "fan1_input": 1180, "fan2_input": 0, "in0_input": 1104,
                                      "curr1_input": 2500, "temp2_input": "garbage"})
        write(self.hwmon / "hwmon2", {"name": "amdgpu", "power1_average": 45_000_000, "power1_input": 44_000_000,
                                      "freq1_input": 2_500_000_000})
        with mock.patch.object(sensors, "battery_sensors", return_value=[]):
            reading = self.source().read()
        by = {(s.group, s.name): s for s in reading.sensors}
        self.assertAlmostEqual(by[("k10temp", "Tctl")].value, 61.25)
        self.assertEqual(by[("k10temp", "Tctl")].unit, "°C")
        self.assertEqual(by[("nct6799", "fan1")].value, 1180)
        self.assertEqual(by[("nct6799", "fan2")].value, 0)  # a measured zero stays zero
        self.assertAlmostEqual(by[("nct6799", "in0")].value, 1.104)
        self.assertAlmostEqual(by[("nct6799", "curr1")].value, 2.5)
        self.assertIsNone(by[("nct6799", "temp2")].value)  # unreadable is unknown, not zero
        self.assertAlmostEqual(by[("amdgpu", "power1")].value, 44.0)  # _input wins over _average for one sensor
        self.assertEqual(len([s for s in reading.sensors if s.group == "amdgpu" and s.kind == "power"]), 1)
        self.assertAlmostEqual(by[("amdgpu", "freq1")].value, 2500)
        self.assertTrue(all(s.source == "hwmon" for s in reading.sensors))

    @unittest.skipIf(sys.platform == "win32", "RAPL zone names contain ':', which Windows paths cannot")
    def test_rapl_power_is_a_rate_and_survives_wraparound(self):
        zone = self.rapl / "intel-rapl:0"
        write(zone, {"name": "package-0", "energy_uj": 1_000_000, "max_energy_range_uj": 10_000_000})
        src = self.source()
        with mock.patch.object(sensors, "battery_sensors", return_value=[]), \
                mock.patch.object(sensors.time, "monotonic", side_effect=[100.0, 102.0, 104.0]):
            first = src.read()
            (zone / "energy_uj").write_text("9000000\n")  # +8 J in 2 s
            second = src.read()
            (zone / "energy_uj").write_text("1000000\n")  # wraps: +2 J in 2 s
            third = src.read()
        package = lambda r: next(s for s in r.sensors if s.source == "rapl")  # noqa: E731
        self.assertIsNone(package(first).value)  # a rate needs two readings
        self.assertAlmostEqual(package(second).value, 4.0)
        self.assertAlmostEqual(package(third).value, 1.0)
        self.assertEqual(package(second).name, "Package power")

    @unittest.skipIf(sys.platform == "win32", "RAPL zone names contain ':', which Windows paths cannot")
    def test_rapl_permission_is_explained(self):
        write(self.rapl / "intel-rapl:0", {"name": "package-0"})
        with mock.patch.object(Path, "read_text", side_effect=PermissionError), \
                mock.patch.object(sensors, "battery_sensors", return_value=[]):
            src = self.source()
            src._hwmon = lambda: []
            src._battery_power = lambda: []
            src._rapl_root = self.rapl
            reading = src.read()
        self.assertTrue(any("root" in note for note in reading.notes))
        self.assertFalse(any(s.source == "rapl" for s in reading.sensors))

    def test_no_hwmon_is_explained(self):
        with mock.patch.object(sensors, "battery_sensors", return_value=[]):
            reading = self.source().read()
        self.assertEqual(reading.sensors, [])
        self.assertTrue(reading.notes)

    def test_session_min_max(self):
        write(self.hwmon / "hwmon0", {"name": "coretemp", "temp1_input": 50000, "temp1_label": "Package id 0"})
        src = self.source()
        with mock.patch.object(sensors, "battery_sensors", return_value=[]):
            src.read()
            (self.hwmon / "hwmon0" / "temp1_input").write_text("70000\n")
            src.read()
            (self.hwmon / "hwmon0" / "temp1_input").write_text("60000\n")
            s = src.read().sensors[0]
        self.assertEqual((s.value, s.min, s.max), (60.0, 50.0, 70.0))


LHM_LINE = {
    "t": 1.0, "admin": True, "pawnio": True, "error": None,
    "hardware": [
        {"id": "/amdcpu/0", "name": "AMD Ryzen 9 7950X", "type": "Cpu", "parent": None, "sensors": [
            {"id": "/amdcpu/0/temperature/2", "name": "Core (Tctl/Tdie)", "type": "Temperature", "value": 71.5, "min": 40, "max": 88},
            {"id": "/amdcpu/0/power/0", "name": "Package", "type": "Power", "value": 120.25, "min": 30, "max": 180},
            {"id": "/amdcpu/0/clock/1", "name": "Core #1", "type": "Clock", "value": None, "min": None, "max": None},
        ]},
        {"id": "/motherboard", "name": "ROG STRIX B650E-F", "type": "Motherboard", "parent": None, "sensors": []},
        {"id": "/lpc/nct6799d/0", "name": "Nuvoton NCT6799D", "type": "SuperIO", "parent": "/motherboard", "sensors": [
            {"id": "/lpc/nct6799d/0/fan/0", "name": "CPU Fan", "type": "Fan", "value": 1450, "min": 0, "max": 1600},
            {"id": "/lpc/nct6799d/0/fan/1", "name": "Fan #2", "type": "Fan", "value": 0, "min": 0, "max": 0},
        ]},
    ],
}


class LhmTests(unittest.TestCase):
    def test_parse_keeps_unknown_and_names_parents(self):
        found, status = parse_lhm(json.dumps(LHM_LINE))
        self.assertTrue(status["admin"] and status["pawnio"])
        by = {s.name: s for s in found}
        self.assertEqual(by["Core (Tctl/Tdie)"].kind, "temperature")
        self.assertEqual(by["Package"].unit, "W")
        self.assertIsNone(by["Core #1"].value)
        self.assertEqual(by["CPU Fan"].hardware, "ROG STRIX B650E-F / Nuvoton NCT6799D")
        self.assertTrue(all(s.source == "lhm" for s in found))

    def test_notes_explain_missing_driver_and_elevation(self):
        found, status = parse_lhm({**LHM_LINE, "admin": False, "pawnio": False, "hardware": []})
        notes = " ".join(lhm_notes(status, found))
        self.assertIn("PawnIO", notes)
        self.assertIn("administrator", notes)
        found, status = parse_lhm(LHM_LINE)
        self.assertEqual(lhm_notes(status, found), [])

    def test_headline_picks(self):
        found, _ = parse_lhm(LHM_LINE)
        reading = SensorReading(found, [], "lhm")
        self.assertEqual(cpu_temperature(reading).value, 71.5)
        self.assertEqual(package_power(reading).value, 120.25)
        self.assertEqual([s.name for s in fans(reading)], ["CPU Fan"])  # a stopped fan is not spinning

    def test_summary_absent_without_sensors(self):
        sample = mock.Mock(sensors=SensorReading([], ["none"]))
        self.assertIsNone(sensor_summary(sample))

    def test_helper_only_used_when_present(self):
        with mock.patch.object(sensors.sys, "platform", "win32"), mock.patch.object(sensors, "helper_path", return_value=None):
            src = sensors.open_sensors()
        self.assertIsInstance(src, sensors.NoSensors)
        self.assertIn("helper", src.read().notes[0])

    def test_sensor_text(self):
        self.assertEqual(Sensor("k", "h", "g", "n", "voltage", 1.10432, "V", "lhm").text(), "1.104 V")
        self.assertEqual(Sensor("k", "h", "g", "n", "fan", 1180.4, "RPM", "lhm").text(), "1180 RPM")
        self.assertEqual(Sensor("k", "h", "g", "n", "temperature", None, "°C", "lhm").text(), "n/a")
        self.assertEqual(Sensor("k", "h", "g", "n", "throughput", 2.5e6, "B/s", "lhm").text(), "2.5 MB/s")


class MachineTests(unittest.TestCase):
    def test_rate_needs_two_readings_and_ignores_resets(self):
        self.assertIsNone(machine._rate(100, None, 1.0))
        self.assertIsNone(machine._rate(50, 100, 1.0))
        self.assertIsNone(machine._rate(100, 50, 0.0))
        self.assertEqual(machine._rate(300, 100, 2.0), 100.0)

    def test_sampler_rates_start_unknown(self):
        with machine.HardwareSampler(sensors=False) as sampler:
            first = sampler.sample()
            second = sampler.sample()
        self.assertIsNone(first.cpu.util_pct)
        self.assertTrue(all(d.read_bps is None for d in first.disks))
        self.assertTrue(all(n.rx_bps is None for n in first.nics))
        d = machine.to_dict(second)
        self.assertEqual(set(d), {"cpu", "memory", "disks", "volumes", "network", "sensors", "sensor_source", "notes", "cost_ms"})
        self.assertGreater(d["memory"]["total"], 0)
        json.dumps(d)  # serializable
        self.assertIn("Hardware", machine.to_text(second))

    def test_windows_disk_active_time_maps_physical_drives(self):
        sampler = machine.HardwareSampler.__new__(machine.HardwareSampler)
        sampler._disk_names, sampler._last_disk = None, {}
        counters = {"PhysicalDrive0": mock.Mock(read_bytes=10, write_bytes=20, busy_time=0),
                    "PhysicalDrive1": mock.Mock(read_bytes=0, write_bytes=0, busy_time=0)}
        with mock.patch.object(machine.psutil, "disk_io_counters", return_value=counters), \
                mock.patch.object(machine.sys, "platform", "win32"):
            disks = sampler._disks(1.0, {"idle": [("0 C: D:", 75.0), ("1 E:", 100.0), ("_Total", 87.5)]})
        self.assertEqual([(d.name, d.active_pct) for d in disks], [("PhysicalDrive0", 25.0), ("PhysicalDrive1", 0.0)])


if __name__ == "__main__":
    unittest.main()
