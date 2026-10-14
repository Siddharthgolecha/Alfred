"""Jetson-specific offline meeting CLI; does not modify swap or require cloud APIs."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys
import time

from ..audio import pactl_json,source_status
from ..config import load_config
from .storage import GuardedSSD,StorageSafetyError
from .capture import Source,Recorder
from .minutes import merge_channels,MinutesReasoner,save_meeting_pdf,pretty_transcript
from .sharing import create_qr_handoff,send_bluetooth_opp


def _source(raw):
    # Alice=alsa_input.usb_... optionally Alice=source,offset_ms,drift_ppm
    if "=" not in raw:
        raise ValueError("--mic must be Speaker=pulse_source[,offset_ms[,drift_ppm]]")
    name,part=raw.split("=",1)
    fields=part.split(",")
    if len(fields)>3:
        raise ValueError("Too many calibration fields")
    return Source(name,fields[0],int(fields[1]) if len(fields)>1 else 0,
                  int(fields[2]) if len(fields)>2 else 0)


def _manifest(folder,ssd):
    folder=Path(folder).resolve(strict=True)
    if not ssd._inside(folder) or folder.parent!=ssd.directory("meetings"):
        raise StorageSafetyError("Session must reside directly under SSD/alfred/meetings")
    manifest=folder/"capture_manifest.json"
    if not manifest.is_file() or manifest.is_symlink():
        raise StorageSafetyError("Capture manifest missing or symlinked")
    records=json.loads(manifest.read_text(encoding="utf-8"))
    channels=records.get("channels") or {}
    for entry in channels.values():
        wav=Path(entry["wav"]).resolve(strict=True)
        if wav.parent!=folder or not wav.is_file():
            raise StorageSafetyError("Recordings cannot escape meeting directory")
    return records


def process_session(ssd,folder,cfg,title,recipient=None,whistle=None,reasoner=None):
    """Offline postprocessing. Re-run after interruption with --resume SESSION."""
    ssd.check(require_ram=True)
    manifest=_manifest(folder,ssd)
    segments=merge_channels(manifest["channels"],whistle=whistle)
    if not segments:
        raise RuntimeError("Whistle found no usable speech; cannot invent meeting minutes")
    output=Path(folder)/"transcript.json"
    output.write_text(json.dumps({"segments":segments,
        "timing_warning":"window bounds are sample-aligned estimates, not exact word timestamps"},
        indent=2),encoding="utf-8")
    output.chmod(0o600)
    txt=Path(folder)/"transcript.txt"
    txt.write_text(pretty_transcript(segments)+"\n",encoding="utf-8")
    txt.chmod(0o600)
    ssd.check()
    minutes=(reasoner or MinutesReasoner(cfg["qwen"])).summarize(segments,meeting_title=title)
    minutes_json=Path(folder)/"minutes.json"
    minutes_json.write_text(json.dumps(minutes,ensure_ascii=False,indent=2),encoding="utf-8")
    minutes_json.chmod(0o600)
    pdf=save_meeting_pdf(ssd,folder,minutes,segments)
    pdf.chmod(0o600)
    ssd.check()
    # QR created immediately after PDF save, before optional OPP transfer.
    qr,metadata=create_qr_handoff(pdf,folder)
    result={"session":str(folder),"pdf":str(pdf),"qr":str(qr),
            "transcript":str(txt),"sha256":metadata["sha256"],
            "transfer":"not started; QR alone does not send a Bluetooth file"}
    if recipient:
        # User explicitly supplies a MAC, and receiver must already be trusted.
        result["transfer"]=send_bluetooth_opp(pdf,recipient)
    return result


def build_parser():
    p=argparse.ArgumentParser(prog="alfred-jetson",description=
       "External ext4 SSD diagnostics and offline multi-mic meeting minutes")
    p.add_argument("--ssd",required=True,help="Existing ext4 SSD mountpoint")
    p.add_argument("--swapfile",help="Existing SSD swapfile to verify (read-only)")
    p.add_argument("--min-free-gib",type=float,default=2.0)
    p.add_argument("--min-ram-mib",type=int,default=384)
    p.add_argument("--require-active-swap",action="store_true")
    p.add_argument("--config",default="config.json")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("diagnose",help="Show guarded SSD, swap and memory health")
    sub.add_parser("caches",help="Set up HF hub/datasets and tmp caches on SSD")
    sub.add_parser("sources",help="List current Pulse/PipeWire microphones")
    record=sub.add_parser("meeting",help="Capture, transcribe and generate PDF/QR offline")
    record.add_argument("--mic",action="append",required=True,
                        help="Speaker=pulse_source[,offset_ms[,drift_ppm]] (repeat)")
    record.add_argument("--seconds",type=int,required=True)
    record.add_argument("--title",default="Meeting Minutes")
    record.add_argument("--consent",action="store_true",
                        help="Confirm recording permission/participant notice")
    record.add_argument("--recipient",help="Explicit paired+trusted OPP recipient MAC")
    send=sub.add_parser("send",help="Send existing PDF to a paired trusted Bluetooth OPP receiver")
    send.add_argument("--session",required=True)
    send.add_argument("--recipient",required=True,
                      help="Already paired and trusted receiver MAC (recipient must accept)")
    resume=sub.add_parser("resume",help="Finish a previous captured session")
    resume.add_argument("--session",required=True)
    resume.add_argument("--title",default="Meeting Minutes")
    resume.add_argument("--recipient",help="Explicit paired+trusted OPP recipient MAC")
    return p


def main(argv=None):
    args=build_parser().parse_args(argv)
    os.umask(0o077)  # Meeting recordings, transcripts and PDFs are private.
    try:
        ssd=GuardedSSD(args.ssd,args.swapfile,args.min_free_gib,
                       args.min_ram_mib,args.require_active_swap)
        if args.command=="diagnose":
            print(json.dumps(ssd.check().json(),indent=2))
            return 0
        if args.command=="caches":
            print(json.dumps(ssd.cache_environment(),indent=2))
            print("Set these environment variables before launching Alfred/Hugging Face downloads.")
            return 0
        if args.command=="sources":
            for src in pactl_json("sources"):
                ok,why=source_status(src,explicitly_selected=True)
                print(json.dumps({"source":src.get("name"),"description":
                    (src.get("properties") or {}).get("device.description"),
                    "recordable_candidate":ok,"reason":why}))
            return 0
        if args.command=="send":
            folder=Path(args.session).resolve(strict=True)
            # Reuse the original session and checksum; do not spend RAM
            # retranscribing/replanning just to push an existing PDF.
            _manifest(folder,ssd)
            pdf=(folder/"minutes.pdf").resolve(strict=True)
            qr=(folder/"handoff.json").resolve(strict=True)
            if pdf.parent!=folder or qr.parent!=folder or not pdf.is_file():
                raise StorageSafetyError("No valid existing meeting PDF for Bluetooth handoff")
            import hashlib
            hasher=hashlib.sha256()
            with open(pdf,"rb") as stream:
                for chunk in iter(lambda:stream.read(65536),b""):
                    hasher.update(chunk)
            digest=hasher.hexdigest()
            if json.loads(qr.read_text(encoding="utf-8")).get("sha256")!=digest:
                raise StorageSafetyError("PDF changed since QR generation; transfer refused")
            result=send_bluetooth_opp(pdf,args.recipient)
            print(json.dumps(result,indent=2))
            return 0
        cfg=load_config(Path(args.config) if Path(args.config).exists() else None)
        if args.command=="meeting":
            if not args.consent:
                raise PermissionError("Use --consent only after informing/obtaining participant permission")
            src=[_source(x) for x in args.mic]
            folder,_=Recorder(ssd,src,args.seconds).record()
            print("Recorded channels on SSD:",folder,flush=True)
        else:
            folder=Path(args.session)
        result=process_session(ssd,folder,cfg,args.title,recipient=args.recipient)
        print(json.dumps(result,indent=2))
        return 0
    except (StorageSafetyError,ValueError,PermissionError,RuntimeError,OSError,
            KeyError,ImportError) as exc:
        print(f"Jetson workflow stopped safely: {exc}",file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
