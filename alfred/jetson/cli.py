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
from .discovery import discover_mics,MAX_MICS
from .calibration import calibrate_claps
from .console import MeetingDisplay
from .live import LiveMeetingPreview
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


def process_session(ssd,folder,cfg,title,recipient=None,whistle=None,reasoner=None,
                    calibrate=False,require_two_claps=False,show_channels=True):
    """Generate a time-merged, overlap-aware transcript and local PDF/QR."""
    ssd.check(require_ram=True)
    manifest=_manifest(folder,ssd)
    channels=manifest["channels"]
    if not channels:
        raise RuntimeError("Meeting contains no captured microphone channels")
    calibration_file=Path(folder)/"calibration.json"
    if calibrate:
        calibrations=calibrate_claps(channels,require_two=require_two_claps)
        calibration_file.write_text(json.dumps(calibrations,indent=2),encoding="utf-8")
        calibration_file.chmod(0o600)
    elif calibration_file.is_file() and not calibration_file.is_symlink():
        calibrations=json.loads(calibration_file.read_text(encoding="utf-8"))
    else:
        calibrations={}
    view=MeetingDisplay(sorted(channels),enabled=show_channels)
    try:
        raw=merge_channels(channels,whistle=whistle,calibrations=calibrations,
                           on_event=view.event,include_crosstalk=True)
    finally:
        view.finish()
    segments=[row for row in raw if row.get("included_in_minutes",True)]
    if not segments:
        raise RuntimeError("Whistle found no usable speech; cannot invent meeting minutes")
    out=Path(folder)
    transcript_info={
        "channels":list(sorted(channels)),
        "microphone_count":len(channels),
        "calibration":calibrations,
        "precision_warning":"Decoder word timing and cross-device alignment are estimates; "
            "independent USB/BT inputs do not share a clock simply because they share a hub.",
        "segments":segments,
        "raw_segments":raw,
        "cross_talk_candidates":sum(1 for row in raw if row.get("likely_crosstalk")),
        "overlap_events":sum(1 for row in segments if row.get("overlap"))
    }
    output=out/"transcript.json"
    output.write_text(json.dumps(transcript_info,indent=2),encoding="utf-8")
    output.chmod(0o600)
    txt=out/"transcript.txt"
    txt.write_text(pretty_transcript(segments)+"\n",encoding="utf-8")
    txt.chmod(0o600)
    ssd.check()
    minutes=(reasoner or MinutesReasoner(cfg["qwen"])).summarize(
        segments,meeting_title=title)
    minutes_json=out/"minutes.json"
    minutes_json.write_text(json.dumps(minutes,ensure_ascii=False,indent=2),
                            encoding="utf-8")
    minutes_json.chmod(0o600)
    pdf=save_meeting_pdf(ssd,folder,minutes,segments)
    pdf.chmod(0o600)
    ssd.check()
    qr,metadata=create_qr_handoff(pdf,folder)
    result={"session":str(folder),"pdf":str(pdf),"qr":str(qr),
            "transcript":str(txt),"structured_transcript":str(output),
            "microphone_count":len(channels),"speaker_channels":list(sorted(channels)),
            "overlap_events":transcript_info["overlap_events"],
            "cross_talk_candidates":transcript_info["cross_talk_candidates"],
            "sha256":metadata["sha256"],
            "transfer":"not started; QR alone does not send a Bluetooth file"}
    if recipient:
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
    record.add_argument("--mic",action="append",
                        help="Explicit Speaker=pulse_source[,offset_ms[,drift_ppm]]; "
                             "omit to auto-detect connected USB/BT mics")
    record.add_argument("--max-mics",type=int,default=MAX_MICS,
                        help="Maximum automatically detected working mics (1..8)")
    record.add_argument("--clap-sync",action="store_true",
                        help="Align clocks using intentional shared calibration claps")
    record.add_argument("--two-claps",action="store_true",
                        help="Require start/end shared claps for drift calibration")
    record.add_argument("--no-channel-view",action="store_true",
                        help="Suppress per-microphone CLI transcript")
    record.add_argument("--live-preview",action="store_true",
                        help="Best-effort concurrent ASR subtitles while recording; "
                             "bounded queue, uses extra RAM/CPU")
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
    resume.add_argument("--clap-sync",action="store_true",
                        help="Recalculate clock calibration from recorded claps")
    resume.add_argument("--two-claps",action="store_true",
                        help="Require start/end shared claps to align clocks")
    resume.add_argument("--no-channel-view",action="store_true",
                        help="Suppress per-microphone console transcript")
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
            listed=pactl_json("sources")
            for src in listed:
                ok,why=source_status(src)
                print(json.dumps({"source":src.get("name"),"description":
                    (src.get("properties") or {}).get("device.description"),
                    "eligible":ok,"reason":why}))
            detected=discover_mics(pulse_sources=listed)
            print(json.dumps({"detected_microphone_count":len(detected),
                 "channels":[{"label":x.speaker,"source":x.pulse_source} for x in detected]}))
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
            if args.two_claps and not args.clap_sync:
                raise ValueError("--two-claps requires --clap-sync")
            src=[_source(x) for x in args.mic] if args.mic else discover_mics(limit=args.max_mics)
            if not src:
                raise RuntimeError("No working USB/Bluetooth microphone discovered; "
                    "connect microphones or provide explicit --mic sources")
            print("Meeting microphone count:",len(src),flush=True)
            for selected in src:
                print(f"  {selected.speaker}: {selected.pulse_source}",flush=True)
            preview=None
            if args.live_preview:
                preview=LiveMeetingPreview([x.speaker for x in src])
                preview.start()
            try:
                folder,_=Recorder(ssd,src,args.seconds,
                    on_preview=preview.submit if preview else None).record()
            finally:
                if preview:
                    preview_status=preview.close()
                    print("Live preview statistics:",json.dumps(preview_status),flush=True)
            print("Recorded channels on SSD:",folder,flush=True)
        else:
            folder=Path(args.session)
        if args.two_claps and not args.clap_sync:
            raise ValueError("--two-claps requires --clap-sync")
        result=process_session(ssd,folder,cfg,args.title,recipient=args.recipient,
            calibrate=args.clap_sync,require_two_claps=args.two_claps,
            show_channels=not args.no_channel_view)
        print(json.dumps(result,indent=2))
        return 0
    except (StorageSafetyError,ValueError,PermissionError,RuntimeError,OSError,
            KeyError,ImportError) as exc:
        print(f"Jetson workflow stopped safely: {exc}",file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
