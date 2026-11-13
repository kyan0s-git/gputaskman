"""In-app updates from this project's GitHub releases.

Nothing here runs unless the user asks (the ``u`` key, ``--check-update``, or ``gputaskman update``): checking is
one HTTPS GET to the GitHub releases API, with a generic User-Agent and nothing about the machine. An installed
copy (the Windows installer) can update itself: the matching ``-setup.exe`` is downloaded over HTTPS, checked
against the release's ``.sha256`` file and started; the installer closes gputaskman, replaces it and starts it
again. The SHA-256 check catches a corrupted or truncated download; the builds are not code-signed, so it is not
proof of who built the file. Other installs (pip/uv, portable archives) are told how to upgrade instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO = "kyan0s-git/gputaskman"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
USER_AGENT = "gputaskman-updater"
TIMEOUT_S = 15.0
MAX_DOWNLOAD = 512 * 2**20  # a setup program larger than this is not ours
CHUNK = 1 << 16


class UpdateError(Exception):
    pass


@dataclass
class Asset:
    name: str
    url: str
    size: int


@dataclass
class Release:
    version: str
    tag: str
    url: str
    notes: str
    assets: list[Asset] = field(default_factory=list)

    def asset(self, name: str) -> Asset | None:
        return next((a for a in self.assets if a.name == name), None)


@dataclass
class UpdateStatus:
    current: str
    latest: Release | None
    kind: str            # installer, portable-windows, portable-linux, package
    newer: bool

    @property
    def summary(self) -> str:
        if self.latest is None:
            return f"gputaskman {self.current}: no published release found at {RELEASES_PAGE}"
        if not self.newer:
            return f"gputaskman {self.current} is up to date (latest release {self.latest.version})"
        return f"gputaskman {self.latest.version} is available (you have {self.current})"


def current_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("gputaskman")
    except PackageNotFoundError:
        return "0"


def parse_version(text: str) -> tuple:
    """A comparable key: 1.2.10 > 1.2.9; a pre-release (1.3.0rc1, 1.3.0-beta) sorts before its release."""
    text = text.strip().lstrip("vV")
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", text)
    if not m:
        return ((0,), 0, text)
    nums = tuple(int(x) for x in m[1].split("."))
    while len(nums) > 1 and nums[-1] == 0:
        nums = nums[:-1]  # 1.2 == 1.2.0
    suffix = m[2].lstrip("-.+_")
    return (nums, 0 if suffix else 1, suffix)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def arch() -> str:
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "x64"


def install_kind(executable: str | None = None, frozen: bool | None = None, system: str | None = None) -> str:
    """How this copy was installed, which decides how it can be updated."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    system = sys.platform if system is None else system
    exe = Path(executable or sys.executable)
    if not frozen:
        return "package"
    if system == "win32":
        return "installer" if any(exe.parent.glob("unins*.exe")) else "portable-windows"
    return "portable-linux"


def installer_name(version: str, cpu: str | None = None) -> str:
    return f"gputaskman-{version}-windows-{cpu or arch()}-setup.exe"


def how_to_upgrade(status: UpdateStatus) -> str:
    v = status.latest.version if status.latest else "latest"
    if status.kind == "installer":
        return "Press i to download and install it."
    if status.kind == "portable-windows":
        return (f"Download gputaskman-{v}-windows-{arch()}.zip (or the -setup.exe installer, which can update itself "
                f"later) from {RELEASES_PAGE} and replace this folder.")
    if status.kind == "portable-linux":
        cpu = "arm64" if arch() == "arm64" else "x64"
        return f"Download gputaskman-{v}-linux-{cpu}.tar.gz from {RELEASES_PAGE} and extract it over this folder."
    return (f"Upgrade with: uv tool install --force git+https://github.com/{REPO}@v{v}  "
            f"(or pipx install --force with the same URL)")


def _get(url: str, accept: str = "application/json", timeout: float = TIMEOUT_S):
    if not url.startswith("https://"):
        raise UpdateError(f"refusing a non-HTTPS URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - https only, checked above
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise UpdateError("no published release found") from error
        if error.code == 403:
            raise UpdateError("GitHub refused the request (HTTP 403: an API rate limit, or a proxy or firewall "
                              "blocking api.github.com); try again later") from error
        raise UpdateError(f"GitHub returned HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise UpdateError(f"could not reach GitHub: {reason}") from error


def parse_release(data: dict) -> Release:
    tag = str(data.get("tag_name") or "")
    if not tag:
        raise UpdateError("the release has no tag")
    assets = [Asset(str(a.get("name", "")), str(a.get("browser_download_url", "")), int(a.get("size") or 0))
              for a in data.get("assets") or []]
    return Release(tag.lstrip("vV"), tag, str(data.get("html_url") or RELEASES_PAGE), str(data.get("body") or ""), assets)


def latest_release() -> Release:
    with _get(API, "application/vnd.github+json") as response:
        try:
            data = json.loads(response.read(2 * 2**20).decode("utf-8"))
        except ValueError as error:
            raise UpdateError("GitHub sent an unreadable reply") from error
    if data.get("draft") or data.get("prerelease"):
        raise UpdateError("the latest release is not a final release")
    return parse_release(data)


def check() -> UpdateStatus:
    current = current_version()
    release = latest_release()
    return UpdateStatus(current, release, install_kind(), is_newer(release.version, current))


def expected_sha256(text: str, name: str) -> str:
    """The digest for ``name`` from a ``sha256sum``-style file ("<hex>  <name>")."""
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
        if len(parts) == 1 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    raise UpdateError(f"the checksum file does not list {name}")


def download_installer(release: Release, dest: Path | None = None, progress=None) -> Path:
    """Download and verify the installer for this machine. ``progress(done, total)`` is called while downloading."""
    name = installer_name(release.version)
    asset, checksum = release.asset(name), release.asset(f"{name}.sha256")
    if asset is None:
        raise UpdateError(f"release {release.tag} has no {name}")
    if checksum is None:
        raise UpdateError(f"release {release.tag} has no checksum for {name}; not installing an unverified file")
    if asset.size > MAX_DOWNLOAD:
        raise UpdateError(f"{name} is unexpectedly large ({asset.size} bytes)")
    with _get(checksum.url, "application/octet-stream") as response:
        want = expected_sha256(response.read(4096).decode("utf-8", errors="replace"), name)
    folder = dest or Path(tempfile.mkdtemp(prefix="gputaskman-update-"))
    target = folder / name
    digest = hashlib.sha256()
    done = 0
    with _get(asset.url, "application/octet-stream", timeout=60.0) as response, open(target, "wb") as out:
        while True:
            chunk = response.read(CHUNK)
            if not chunk:
                break
            done += len(chunk)
            if done > MAX_DOWNLOAD:
                raise UpdateError("download exceeded the size limit")
            digest.update(chunk)
            out.write(chunk)
            if progress:
                progress(done, asset.size)
    if asset.size and done != asset.size:
        target.unlink(missing_ok=True)
        raise UpdateError(f"download incomplete ({done} of {asset.size} bytes)")
    if digest.hexdigest() != want:
        target.unlink(missing_ok=True)
        raise UpdateError("SHA-256 mismatch: the download is corrupt or was changed; nothing was installed")
    return target


def launch_installer(path: Path) -> None:
    """Start the installer detached. /RELAUNCH=1 makes it start gputaskman again when it finishes."""
    if sys.platform != "win32":
        raise UpdateError("self-update is only available for the Windows installer")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1"],
                     close_fds=True, creationflags=flags, cwd=os.path.dirname(path))


def main(argv: list[str]) -> int:
    """``gputaskman update [--check] [--yes]``."""
    import argparse

    ap = argparse.ArgumentParser(prog="gputaskman update", description=f"Check {RELEASES_PAGE} for a newer release "
                                 "and, for the Windows installer build, download, verify and install it.")
    ap.add_argument("--check", action="store_true", help="only report whether an update is available")
    ap.add_argument("-y", "--yes", action="store_true", help="install without asking")
    args = ap.parse_args(argv)
    try:
        status = check()
    except UpdateError as error:
        print(f"Update check failed: {error}", file=sys.stderr)
        return 2
    print(status.summary)
    if not status.newer or args.check:
        return 0
    if status.kind != "installer":
        print(how_to_upgrade(status))
        return 0
    if not args.yes:
        answer = input(f"Download and install gputaskman {status.latest.version}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            return 0
    try:
        def show(done: int, total: int) -> None:
            print(f"\rDownloading {done / 2**20:.1f} / {total / 2**20:.1f} MB", end="", flush=True)

        path = download_installer(status.latest, progress=show)
        print(f"\nVerified {path.name}; starting the installer.")
        launch_installer(path)
    except UpdateError as error:
        print(f"\nUpdate failed: {error}", file=sys.stderr)
        return 1
    return 0
