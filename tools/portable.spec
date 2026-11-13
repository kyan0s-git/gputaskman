import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).parent
icon = str(root / "tools" / "assets" / "gputaskman.ico")
analysis = Analysis(
    [str(root / "tools" / "portable_entry.py")],
    pathex=[str(root / "src")],
    datas=collect_data_files("textual") + copy_metadata("gputaskman", recursive=True),
)

# These belong to the operating system or the installed NVIDIA driver, not the portable app.
driver_libs = ("nvml.dll", "nvcuda.dll", "atiadlxx.dll", "amd_smi.dll", "libamd_smi.so", "dxgi.dll", "pdh.dll", "libnvidia-ml.so", "libcuda.so", "libdxcore.so")


def is_driver_lib(name):
    name = name.lower()
    return any(name == lib or name.startswith(lib + ".") for lib in driver_libs)


analysis.binaries = [entry for entry in analysis.binaries if not is_driver_lib(Path(entry[0]).name)]

pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="gputaskman",
    console=True,
    icon=icon,
)
parts = [exe, analysis.binaries, analysis.datas]

if sys.platform == "win32":
    # "GPU Task Manager.exe": a windowless launcher that opens the TUI in a console window of its own.
    launcher = Analysis([str(root / "tools" / "launcher_entry.py")], pathex=[str(root / "src")])
    launcher.binaries = [entry for entry in launcher.binaries if not is_driver_lib(Path(entry[0]).name)]
    launcher_exe = EXE(
        PYZ(launcher.pure),
        launcher.scripts,
        [],
        exclude_binaries=True,
        name="GPU Task Manager",
        console=False,
        icon=icon,
    )
    parts += [launcher_exe, launcher.binaries, launcher.datas]

bundle = COLLECT(*parts, name="gputaskman")
