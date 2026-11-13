from pathlib import Path
import struct
import tempfile
import unittest
import zipfile
import hashlib

import os
import stat
import tarfile

from unittest import mock

from tools import build_portable
from tools.build_portable import (architecture, check_elf, check_executable, create_archive, create_linux_archive,
                                  nvidia_license, target, validate_version)


class PortableTests(unittest.TestCase):
    def write_executable(self, path, machine):
        header = bytearray(64)
        header[:2] = b"MZ"
        struct.pack_into("<I", header, 60, 64)
        path.write_bytes(header + b"PE\0\0" + struct.pack("<H", machine))

    def test_interpreter_architecture_not_host_architecture(self):
        self.assertEqual(architecture("win-amd64"), "x64")
        self.assertEqual(architecture("win-arm64"), "arm64")
        with self.assertRaises(ValueError):
            architecture("win32")

    def test_version_and_tag(self):
        validate_version("0.1.0", "v0.1.0")
        with self.assertRaises(ValueError):
            validate_version("0.1.0", "v0.2.0")
        with self.assertRaises(ValueError):
            validate_version("..\\outside")

    def test_inline_nvidia_license_is_preserved(self):
        notice = "#####\n# Copyright Example\n# Redistribution and use are permitted.\n#####\n"
        self.assertEqual(nvidia_license(notice + "import ctypes\n"), notice)
        with self.assertRaises(ValueError):
            nvidia_license("import ctypes\n")

    def test_executable_machine(self):
        with tempfile.TemporaryDirectory() as temporary:
            exe = Path(temporary) / "gputaskman.exe"
            for arch, machine in [("x64", 0x8664), ("arm64", 0xAA64)]:
                self.write_executable(exe, machine)
                check_executable(exe, arch)
            with self.assertRaises(ValueError):
                check_executable(exe, "x64")
            exe.write_bytes(b"not an exe")
            with self.assertRaises(ValueError):
                check_executable(exe, "x64")

    def test_archive_includes_complete_runtime_and_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            (bundle / "_internal").mkdir(parents=True)
            self.write_executable(bundle / "gputaskman.exe", 0x8664)
            (bundle / "_internal" / "data.css").write_text("resource", encoding="utf-8")
            install_metadata = bundle / "_internal" / "gputaskman-0.1.0.dist-info"
            install_metadata.mkdir()
            (install_metadata / "direct_url.json").write_text(
                '{"url": "file:///private-build-path"}', encoding="utf-8",
            )
            archive = create_archive(bundle, root / "output", "0.1.0", "x64")
            with zipfile.ZipFile(archive) as zipped:
                self.assertEqual(set(zipped.namelist()), {
                    "gputaskman-0.1.0-windows-x64/gputaskman.exe",
                    "gputaskman-0.1.0-windows-x64/_internal/data.css",
                })
            expected = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(
                archive.with_suffix(".zip.sha256").read_text(encoding="utf-8"),
                f"{expected}  {archive.name}\n",
            )

    def test_archive_rejects_driver_dlls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_executable(root / "gputaskman.exe", 0xAA64)
            for dll in ("NVML.dll", "atiadlxx.dll", "amd_smi.dll"):
                (root / dll).write_bytes(b"driver")
                with self.assertRaisesRegex(ValueError, "must not be distributed"):
                    create_archive(root, root / "output", "0.1.0", "arm64")
                os.remove(root / dll)


class LinuxPortableTests(unittest.TestCase):
    def write_elf(self, path, machine):
        header = bytearray(64)
        header[:4] = b"\x7fELF"
        header[4], header[5] = 2, 1  # 64-bit, little-endian
        struct.pack_into("<H", header, 18, machine)
        path.write_bytes(bytes(header))
        path.chmod(0o755)

    def test_linux_interpreters_map_to_targets(self):
        self.assertEqual(target("linux-x86_64"), ("linux", "x64"))
        self.assertEqual(target("linux-aarch64"), ("linux", "arm64"))
        self.assertEqual(architecture("linux-x86_64"), "x64")
        with self.assertRaises(ValueError):
            target("linux-i686")

    def test_elf_machine(self):
        with tempfile.TemporaryDirectory() as temporary:
            exe = Path(temporary) / "gputaskman"
            for arch, machine in [("x64", 0x3E), ("arm64", 0xB7)]:
                self.write_elf(exe, machine)
                check_elf(exe, arch)
            with self.assertRaises(ValueError):
                check_elf(exe, "x64")
            exe.write_bytes(b"MZ not an elf")
            with self.assertRaises(ValueError):
                check_elf(exe, "x64")

    def test_tarball_keeps_executable_bit_and_has_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            (bundle / "_internal").mkdir(parents=True)
            self.write_elf(bundle / "gputaskman", 0x3E)
            (bundle / "_internal" / "data.css").write_text("resource", encoding="utf-8")
            archive = create_linux_archive(bundle, root / "output", "0.1.0", "x64")
            self.assertEqual(archive.name, "gputaskman-0.1.0-linux-x64.tar.gz")
            with tarfile.open(archive) as packed:
                members = {m.name: m for m in packed.getmembers()}
            self.assertEqual(set(members), {"gputaskman-0.1.0-linux-x64/gputaskman",
                                            "gputaskman-0.1.0-linux-x64/_internal/data.css"})
            self.assertTrue(members["gputaskman-0.1.0-linux-x64/gputaskman"].mode & stat.S_IXUSR)
            self.assertEqual(members["gputaskman-0.1.0-linux-x64/gputaskman"].uid, 0)
            expected = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(Path(f"{archive}.sha256").read_text(encoding="utf-8"), f"{expected}  {archive.name}\n")

    def test_tarball_rejects_driver_libraries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_elf(root / "gputaskman", 0x3E)
            for lib in ("libnvidia-ml.so.1", "libcuda.so.1", "libamd_smi.so.0"):
                (root / lib).write_bytes(b"driver")
                with self.assertRaisesRegex(ValueError, "must not be distributed"):
                    create_linux_archive(root, root / "output", "0.1.0", "x64")
                os.remove(root / lib)


class InstallerTests(unittest.TestCase):
    def test_installer_runs_iscc_with_bundle_and_writes_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, out = root / "bundle", root / "out"
            bundle.mkdir()
            exe = bundle / "gputaskman.exe"
            header = bytearray(64)
            header[:2] = b"MZ"
            struct.pack_into("<I", header, 60, 64)
            exe.write_bytes(header + b"PE\0\0" + struct.pack("<H", 0x8664))

            def fake_run(cmd, check):
                self.assertIn("/DAppVersion=1.2.3", cmd)
                self.assertIn("/DArch=x64", cmd)
                (out / "gputaskman-1.2.3-windows-x64-setup.exe").write_bytes(b"setup")

            with mock.patch.object(build_portable, "find_iscc", return_value=Path("ISCC.exe")), \
                    mock.patch.object(build_portable.subprocess, "run", fake_run):
                installer = build_portable.create_installer(bundle, out, "1.2.3", "x64")
            self.assertTrue(Path(f"{installer}.sha256").read_text().startswith(hashlib.sha256(b"setup").hexdigest()))

    def test_installer_rejects_driver_dlls(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            header = bytearray(64)
            header[:2] = b"MZ"
            struct.pack_into("<I", header, 60, 64)
            (bundle / "gputaskman.exe").write_bytes(header + b"PE\0\0" + struct.pack("<H", 0x8664))
            (bundle / "nvml.dll").write_bytes(b"x")
            with self.assertRaises(ValueError):
                build_portable.create_installer(bundle, bundle / "out", "1.2.3", "x64")


class SensorBundleTests(unittest.TestCase):
    def pe(self, path, machine):
        header = bytearray(64)
        header[:2] = b"MZ"
        struct.pack_into("<I", header, 60, 64)
        path.write_bytes(header + b"PE\0\0" + struct.pack("<H", machine))

    def test_kernel_drivers_are_forbidden(self):
        self.assertTrue(build_portable.is_forbidden("PawnIO.sys"))
        self.assertTrue(build_portable.is_forbidden("WinRing0x64.SYS"))
        self.assertFalse(build_portable.is_forbidden("LibreHardwareMonitorLib.dll"))

    def test_pawnio_setup_must_match_the_pinned_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup = Path(tmp) / "PawnIO_setup.exe"
            setup.write_bytes(b"not pawnio")
            with self.assertRaises(ValueError):
                build_portable.check_pawnio(setup)

    def test_sensor_helper_is_copied_and_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper, bundle = root / "helper", root / "bundle"
            helper.mkdir()
            bundle.mkdir()
            self.pe(helper / "gputaskman-sensors.exe", 0x8664)
            (helper / "LibreHardwareMonitorLib.dll").write_bytes(b"x")
            (helper / "gputaskman-sensors.pdb").write_bytes(b"x")
            build_portable.add_sensors(bundle, helper, "x64")
            self.assertTrue((bundle / "sensors" / "LibreHardwareMonitorLib.dll").is_file())
            self.assertFalse((bundle / "sensors" / "gputaskman-sensors.pdb").exists())
            with self.assertRaises(ValueError):  # an arm64 helper in an x64 build
                self.pe(helper / "gputaskman-sensors.exe", 0xAA64)
                build_portable.add_sensors(root / "other", helper, "x64")
            self.pe(helper / "gputaskman-sensors.exe", 0x8664)
            (helper / "PawnIO.sys").write_bytes(b"x")
            with self.assertRaises(ValueError):
                build_portable.add_sensors(root / "third", helper, "x64")


if __name__ == "__main__":
    unittest.main()
