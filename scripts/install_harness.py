"""Verify official pinned DeepSeek Harness wheels for offline Linux setup."""
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
    target = target or ("linux_aarch64" if machine in ("aarch64", "arm64")
                        else "linux_x86_64" if machine in ("x86_64", "amd64")
                        else "")
    if target not in lock["runtime"]:
        raise ValueError("Select linux_x86_64 or linux_aarch64")
    wheels = []
    for row in (lock["sdk"], lock["runtime"][target]):
        name = row["filename"]
        if Path(name).name != name:
            raise ValueError("Unsafe wheel filename")
        path = Path(wheelhouse).resolve() / name
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError("Missing pinned wheel: " + name)
        with path.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        if sha != row["sha256"]:
            raise ValueError("Published SHA-256 mismatch: " + name)
        wheels.append(path)
    return wheels


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse",required=True,type=Path)
    parser.add_argument("--platform",choices=("linux_x86_64","linux_aarch64"))
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--verify",action="store_true")
    action.add_argument("--install",action="store_true")
    args=parser.parse_args(argv)
    try:
        machine=platform.machine().lower()
        local="linux_aarch64" if machine in ("aarch64","arm64") else (
              "linux_x86_64" if machine in ("x86_64","amd64") else "")
        if args.install and (platform.system()!="Linux"
                             or (args.platform and args.platform!=local)):
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
