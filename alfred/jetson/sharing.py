"""Offline QR *handoff metadata* + optional explicit paired-device Bluetooth OPP push.

Scanning a QR alone is NOT a universal Bluetooth file-transfer protocol.
Receiver must explicitly accept an OBEX Object Push on a supported OS.
"""
from __future__ import annotations
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

_MAC=re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")


def adapter_address():
    exe=shutil.which("bluetoothctl")
    if not exe:
        return None
    result=subprocess.run([exe,"show"],capture_output=True,text=True,timeout=8,check=False)
    if result.returncode:
        return None
    hit=re.search(r"\bController\s+([0-9A-Fa-f:]{17})\b",result.stdout)
    return hit.group(1).upper() if hit and _MAC.fullmatch(hit.group(1)) else None


def create_qr_handoff(pdf_path,session_dir,adapter=None):
    """Generate a QR containing a local Bluetooth handoff manifest, not a fake URL."""
    import qrcode
    pdf=Path(pdf_path).resolve(strict=True)
    folder=Path(session_dir).resolve(strict=True)
    if pdf.parent!=folder or pdf.suffix.lower()!=".pdf":
        raise ValueError("Handoff must reference a PDF in this meeting folder")
    address=adapter if adapter is not None else adapter_address()
    if address and not _MAC.fullmatch(address):
        raise ValueError("Invalid Bluetooth adapter MAC")
    digest=hashlib.sha256()
    with open(pdf,"rb") as f:
        for block in iter(lambda:f.read(65536),b""):
            digest.update(block)
    manifest={
        "format":"alfred-bt-handoff-v1",
        "device":address,
        "filename":pdf.name,
        "sha256":digest.hexdigest(),
        "bytes":pdf.stat().st_size,
        "protocol":"Bluetooth OBEX Object Push (explicit accept required)",
        "transfer":"Pair/enable receiving OPP on your phone, then explicitly send from Alfred",
        "note":"QR is handoff metadata; generic camera scan cannot launch file transfer"}
    # Public QR excludes document text; never expose an unprotected endpoint.
    qrfile=folder/"handoff-qr.png"
    img=qrcode.make(json.dumps(manifest,sort_keys=True,separators=(",",":")))
    img.save(qrfile)
    (folder/"handoff.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    qrfile.chmod(0o600)
    return qrfile,manifest


def send_bluetooth_opp(pdf_path,recipient_mac):
    """Explicit user initiated transfer only. Never silently pair with a stranger."""
    if not _MAC.fullmatch(recipient_mac or ""):
        raise ValueError("Expected paired recipient Bluetooth MAC in XX:XX:... format")
    cmd=shutil.which("bt-obex")
    ctl=shutil.which("bluetoothctl")
    if not cmd or not ctl:
        raise RuntimeError("Install bluez-tools (bt-obex) and BlueZ bluetoothctl")
    state=subprocess.run([ctl,"info",recipient_mac],capture_output=True,text=True,
                         timeout=8,check=False)
    if (state.returncode or not re.search(r"^\s*Paired:\s*yes\s*$",state.stdout,re.M)
            or not re.search(r"^\s*Trusted:\s*yes\s*$",state.stdout,re.M)):
        raise PermissionError("Recipient must already be paired and trusted; no auto-pairing")
    pdf=Path(pdf_path).resolve(strict=True)
    if not pdf.is_file() or pdf.suffix.lower()!=".pdf":
        raise ValueError("No valid PDF to transfer")
    # bt-obex performs standard Object Push to the destination device. Support
    # varies: many Android receivers accept it; iOS often does not support OPP.
    send=subprocess.run([cmd,"-p",recipient_mac,str(pdf)],capture_output=True,text=True,
                        timeout=180,check=False)
    if send.returncode:
        raise RuntimeError("Bluetooth OPP rejected/failed: "+send.stderr.strip()[-500:])
    return {"transport":"bluetooth_opp","recipient":recipient_mac.upper(),
            "bytes":pdf.stat().st_size,"status":"client completed; verify on receiving device"}
