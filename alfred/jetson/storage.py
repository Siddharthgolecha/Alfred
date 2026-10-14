"""Read-only inspection of external SSD/swap and bounded, fail-closed storage writes.

Swap is managed by the Linux kernel. This module NEVER creates swapfiles,
changes swappiness, activates swap, or forces hot model pages into swap.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import json
import os
import shutil
import stat
import subprocess
import time


class StorageSafetyError(RuntimeError):
    pass


def _proc_meminfo():
    result={}
    for line in Path("/proc/meminfo").read_text().splitlines():
        parts=line.split()
        if len(parts)>=2 and parts[1].isdigit():
            result[parts[0].rstrip(":")]=int(parts[1])*1024
    return result


def _swaps():
    result=[]
    for line in Path("/proc/swaps").read_text().splitlines()[1:]:
        parts=line.split()
        if len(parts)>=5:
            result.append({"path":parts[0],"size_bytes":int(parts[2])*1024,
                           "used_bytes":int(parts[3])*1024,"priority":int(parts[4])})
    return result


def _mount_at(path):
    """Use findmnt -T so a directory merely existing cannot masquerade as a mount."""
    proc=subprocess.run(["findmnt","--json","--target",str(path),
                         "--output","TARGET,SOURCE,FSTYPE,OPTIONS"],
                        capture_output=True,text=True,timeout=5,check=False)
    if proc.returncode:
        raise StorageSafetyError("findmnt failed: "+proc.stderr.strip()[:160])
    try:
        mounts=json.loads(proc.stdout)["filesystems"]
        return mounts[0]
    except (ValueError,KeyError,IndexError,TypeError) as exc:
        raise StorageSafetyError("Unable to inspect SSD filesystem") from exc


@dataclass
class SSDReport:
    mount: str
    device: str
    filesystem: str
    free_bytes: int
    memory_available_bytes: int
    swap: list
    warnings: list

    def json(self):
        return asdict(self)


class GuardedSSD:
    def __init__(self,mount,swapfile=None,min_free_gib=2.0,
                 min_ram_mib=384,require_active_swap=False):
        # Require an explicit mounted path. No fallback to the Jetson's onboard disk.
        self.mount=Path(mount).expanduser().resolve(strict=True)
        self.swapfile=(Path(swapfile).expanduser().resolve(strict=True)
                       if swapfile else None)
        self.min_free=int(float(min_free_gib)*1024**3)
        self.min_ram=int(float(min_ram_mib)*1024**2)
        self.require_active_swap=bool(require_active_swap)
        self._original_device=self.mount.stat().st_dev
        self._original_mount_source=None
        self.check()

    def _inside(self,path):
        try:
            path.relative_to(self.mount)
            return True
        except ValueError:
            return False

    def check(self,need_bytes=0,require_ram=False):
        if not self.mount.is_dir() or not self.mount.is_mount():
            raise StorageSafetyError(f"SSD not mounted at {self.mount}; refusing onboard fallback")
        if self.mount.stat().st_dev!=self._original_device:
            raise StorageSafetyError("SSD device changed since first validation")
        info=_mount_at(self.mount)
        if Path(info.get("target","/")).resolve()!=self.mount:
            raise StorageSafetyError("Configured SSD path is not a dedicated mount point")
        if info.get("fstype")!="ext4":
            raise StorageSafetyError("External storage must be ext4; found "+str(info.get("fstype")))
        source=info.get("source","")
        if self._original_mount_source is None:
            self._original_mount_source=source
        if source!=self._original_mount_source:
            raise StorageSafetyError("SSD mount source changed; aborting")
        if not os.access(self.mount,os.W_OK):
            raise StorageSafetyError("SSD is not writable for this user")
        free=shutil.disk_usage(self.mount).free
        if free<int(need_bytes)+self.min_free:
            raise StorageSafetyError("SSD free space below reserved margin")
        mem=_proc_meminfo()
        available=mem.get("MemAvailable",0)
        if require_ram and available<self.min_ram:
            raise StorageSafetyError("Not enough physical RAM headroom for this operation")
        swaps=_swaps()
        warnings=[]
        if self.swapfile:
            if not self._inside(self.swapfile):
                raise StorageSafetyError("Configured swapfile must be on this SSD")
            if not self.swapfile.is_file():
                raise StorageSafetyError("Swapfile missing from the SSD")
            matching=[s for s in swaps if Path(s["path"]).resolve()==self.swapfile]
            if not matching:
                message="Configured SSD swapfile not active; continuing without forced swapon"
                if self.require_active_swap:
                    raise StorageSafetyError(message)
                warnings.append(message)
            elif matching[0]["used_bytes"]>matching[0]["size_bytes"]*.85:
                warnings.append("SSD swap utilization above 85%; risk of thrashing")
        if available<self.min_ram:
            warnings.append("Low physical RAM headroom; avoid concurrent heavy inference")
        return SSDReport(str(self.mount),source,"ext4",free,available,swaps,warnings)

    def directory(self,name):
        if not name or "/" in name or name.startswith(".") or ".." in name:
            raise StorageSafetyError("Unsafe storage directory")
        self.check()
        path=(self.mount/"alfred"/name)
        path.mkdir(parents=True,exist_ok=True,mode=0o700)
        resolved=path.resolve(strict=True)
        if not self._inside(resolved) or resolved.stat().st_dev!=self._original_device:
            raise StorageSafetyError("Refusing symlink or mount escape")
        return resolved

    def path(self,folder,filename):
        if not filename or Path(filename).name!=filename or filename.startswith("."):
            raise StorageSafetyError("Unsafe output filename")
        target=self.directory(folder)/filename
        if target.is_symlink():
            raise StorageSafetyError("Refusing symlink destination")
        return target

    def cache_environment(self,apply=False):
        """Only cache/storage paths move to SSD. Live engine/GPU memory does not."""
        hub=self.directory("hf_hub")
        datasets=self.directory("hf_datasets")
        tmp=self.directory("tmp")
        env={"HF_HUB_CACHE":str(hub),"HF_DATASETS_CACHE":str(datasets),
             "TMPDIR":str(tmp)}
        if apply:
            os.environ.update(env)
        return env

    def write_json(self,folder,filename,data):
        self.check()
        output=self.path(folder,filename)
        tmp=self.path(folder,filename+".partial")
        # Every write is guarded; never spill to the onboard filesystem.
        serialized=json.dumps(data,ensure_ascii=False,indent=2).encode()
        self.check(need_bytes=len(serialized))
        with open(tmp,"xb") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        self.check()
        os.replace(tmp,output)
        return output
