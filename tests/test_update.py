import asyncio
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gputaskman import launcher, update, window
from gputaskman.update import Asset, Release, UpdateError


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def release_with(payload: bytes, checksum: str | None = None, version: str = "9.9.9") -> tuple[Release, dict]:
    name = update.installer_name(version)
    digest = checksum if checksum is not None else hashlib.sha256(payload).hexdigest()
    urls = {f"https://example.invalid/{name}": payload,
            f"https://example.invalid/{name}.sha256": f"{digest}  {name}\n".encode()}
    assets = [Asset(name, f"https://example.invalid/{name}", len(payload)),
              Asset(f"{name}.sha256", f"https://example.invalid/{name}.sha256", 80)]
    return Release(version, f"v{version}", "https://example.invalid/release", "notes", assets), urls


class VersionTests(unittest.TestCase):
    def test_ordering(self):
        self.assertTrue(update.is_newer("0.10.0", "0.9.9"))
        self.assertTrue(update.is_newer("v1.0.0", "0.4.0"))
        self.assertFalse(update.is_newer("0.4.0", "0.4.0"))
        self.assertFalse(update.is_newer("0.4", "0.4.0"))
        self.assertTrue(update.is_newer("1.0.0", "1.0.0rc1"))
        self.assertFalse(update.is_newer("1.0.0rc1", "1.0.0"))

    def test_install_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "gputaskman.exe"
            exe.write_bytes(b"")
            self.assertEqual(update.install_kind(str(exe), frozen=True, system="win32"), "portable-windows")
            (Path(tmp) / "unins000.exe").write_bytes(b"")
            self.assertEqual(update.install_kind(str(exe), frozen=True, system="win32"), "installer")
            self.assertEqual(update.install_kind(str(exe), frozen=True, system="linux"), "portable-linux")
            self.assertEqual(update.install_kind(str(exe), frozen=False, system="win32"), "package")

    def test_checksum_file(self):
        digest = "a" * 64
        self.assertEqual(update.expected_sha256(f"{digest}  x-setup.exe\n", "x-setup.exe"), digest)
        self.assertEqual(update.expected_sha256(f"{digest} *x-setup.exe", "x-setup.exe"), digest)
        with self.assertRaises(UpdateError):
            update.expected_sha256(f"{digest}  other.exe\n", "x-setup.exe")

    def test_parse_release(self):
        r = update.parse_release({"tag_name": "v0.5.0", "html_url": "u", "body": "b",
                                  "assets": [{"name": "a", "browser_download_url": "https://x/a", "size": 3}]})
        self.assertEqual((r.version, r.tag, r.asset("a").size), ("0.5.0", "v0.5.0", 3))

    def test_refuses_plain_http(self):
        with self.assertRaises(UpdateError):
            update._get("http://example.invalid/")


class DownloadTests(unittest.TestCase):
    def fetch(self, urls):
        return lambda url, accept="", timeout=0: Response(urls[url])

    def test_verified_download(self):
        release, urls = release_with(b"installer bytes")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(update, "_get", self.fetch(urls)):
            path = update.download_installer(release, Path(tmp))
            self.assertEqual(path.read_bytes(), b"installer bytes")

    def test_mismatch_deletes_and_refuses(self):
        release, urls = release_with(b"installer bytes", checksum="0" * 64)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(update, "_get", self.fetch(urls)):
            with self.assertRaises(UpdateError):
                update.download_installer(release, Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_missing_checksum_is_refused(self):
        release, urls = release_with(b"x")
        release.assets = release.assets[:1]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(update, "_get", self.fetch(urls)):
            with self.assertRaises(UpdateError):
                update.download_installer(release, Path(tmp))

    def test_how_to_upgrade_by_kind(self):
        release, _ = release_with(b"x")
        for kind, needle in (("installer", "Press i"), ("portable-windows", ".zip"),
                             ("portable-linux", ".tar.gz"), ("package", "uv tool install")):
            status = update.UpdateStatus("0.4.0", release, kind, True)
            self.assertIn(needle, update.how_to_upgrade(status))


class LauncherTests(unittest.TestCase):
    def test_conhost_when_present(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "System32").mkdir()
            app = Path(root) / "gputaskman.exe"
            self.assertEqual(launcher.command(app, ["--gpu", "1"], root), [str(app), "--window", "--gpu", "1"])
            (Path(root) / "System32" / "conhost.exe").write_bytes(b"")
            cmd = launcher.command(app, [], root)
            self.assertTrue(cmd[0].endswith("conhost.exe"))
            self.assertEqual(cmd[1:], [str(app), "--window"])

    def test_window_fit(self):
        self.assertEqual(window.fit((170, 52), (300, 90)), (170, 52))
        self.assertEqual(window.fit((170, 52), (120, 40)), (120, 40))
        self.assertEqual(window.fit((170, 52), (60, 10)), (80, 24))
        self.assertEqual(window.fit((170, 52), (0, 0)), (170, 52))


class ScreenTests(unittest.TestCase):
    def test_hardware_and_update_screens(self):
        from gputaskman import app as appmod
        from gputaskman.hwview import HardwareScreen
        from gputaskman.updateview import UpdateScreen

        release, _ = release_with(b"x", version="99.0.0")
        status = update.UpdateStatus("0.4.0", release, "package", True)

        from gputaskman.machine import HardwareSampler

        with HardwareSampler(sensors=False) as sampler:
            sampler.sample()
            sample = sampler.sample()

        async def run():
            gui = appmod.GpuTaskManager(interval=0.2, sensors=False)
            # No GPU collection: this test must behave the same on every CI runner, GPU or not.
            with mock.patch.object(appmod.GpuTaskManager, "poll", lambda self: None), \
                    mock.patch.object(update, "check", return_value=status):
                async with gui.run_test(size=(140, 45)) as pilot:
                    await pilot.pause()
                    gui.hw_sample = sample
                    gui.hw_history.add(sample)
                    await pilot.press("h")
                    await pilot.pause(0.8)
                    self.assertIsInstance(gui.screen, HardwareScreen)
                    self.assertIsNotNone(gui.screen.sample)
                    await pilot.press("f")
                    await pilot.pause(0.3)
                    await pilot.press("escape")
                    await pilot.pause(0.3)
                    self.assertNotIsInstance(gui.screen, HardwareScreen)
                    await pilot.press("u")
                    await pilot.pause(0.8)
                    self.assertIsInstance(gui.screen, UpdateScreen)
                    self.assertTrue(gui.screen.status.newer)
                    await pilot.press("escape")
                    await pilot.pause(0.2)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
