"""Verify pinned DeepSeek Harness wheels for supported offline setup targets."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys

LOCK = Path(__file__).resolve().parents[1] / "coordination" / "deepseek-harness-lock.json"


def verified_wheels(wheelhouse, target=None):
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    if lock["version"] != "0.1.5rc1":
        raise ValueError("Unsupported Harness lock revision")
    machine = platform.machine().lower()
    system = platform.system().lower()
    prefix = {"linux": "linux", "darwin": "macos", "windows": "windows"}.get(system, "")
    arch = {"aarch64": "aarch64", "arm64": "aarch64",
            "x86_64": "x86_64", "amd64": "x86_64"}.get(machine, "")
    local = f"{prefix}_{arch}" if prefix and arch else ""
    target = target or local
    if target not in lock["runtime"]:
        raise ValueError("Select one of the pinned Linux, macOS or Windows runtime targets")
    wheels = []
    for row in (lock["sdk"], lock["runtime"][target]):
        name = row["filename"]
        if Path(name).name != name:
            raise ValueError("Unsafe wheel filename")
        path = Path(wheelhouse).resolve() / name
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError("Missing pinned wheel: " + name)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        sha = digest.hexdigest()
        if sha != row["sha256"]:
            raise ValueError("Published SHA-256 mismatch: " + name)
        wheels.append(path)
    return wheels


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse",required=True,type=Path)
    parser.add_argument("--platform",choices=("linux_x86_64","linux_aarch64",
                                               "macos_x86_64","macos_aarch64",
                                               "windows_x86_64"))
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--verify",action="store_true")
    action.add_argument("--install",action="store_true")
    args=parser.parse_args(argv)
    try:
        system=platform.system().lower()
        machine=platform.machine().lower()
        prefix={"linux":"linux","darwin":"macos","windows":"windows"}.get(system,"")
        arch={"aarch64":"aarch64","arm64":"aarch64",
              "x86_64":"x86_64","amd64":"x86_64"}.get(machine,"")
        local=f"{prefix}_{arch}" if prefix and arch else ""
        if args.install and (not local or (args.platform and args.platform!=local)):
            raise ValueError("Cannot install a wheel for another platform")
        wheels=verified_wheels(args.wheelhouse,args.platform)
        for wheel in wheels:
            print("SHA-256 VERIFIED:",wheel.name,flush=True)
        if args.install:
            subprocess.check_call([sys.executable,"-m","pip","install",
                                   "--no-index","--no-deps",*map(str,wheels)])
        return 0
    except (OSError,ValueError,subprocess.CalledProcessError) as exc:
        print("Harness wheelhouse verification failed:",exc,file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
