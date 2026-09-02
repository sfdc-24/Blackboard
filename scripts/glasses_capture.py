#!/usr/bin/env python3
"""SFDC24 Blackboard - Glasses Intake desktop capture feeder.

claude-code-cli, 2026-09-02. ORDER wf=GLASSES-INTAKE sub=WEBCAM-CAPTURE
(row 2026-09-02T20:16:00Z, from claude-mobile).

Proves the Glasses Intake pipeline with hardware already on the desk: a USB
webcam pointed at the monitor, or -- when the LCD defeats the camera -- a
native screen grab. One frame per wake; Task Scheduler owns the loop so a
crashed wake costs one cycle, not the whole shift (ENTRY 012 precedent).

WHAT THIS MACHINE ACTUALLY HAS (probed 2026-09-02, not assumed):
  Two capture devices enumerate -- "C922 Pro Stream Webcam" and "LG Camera".
  There is no C920 despite the order naming one. OpenCV addresses cameras by
  INDEX and cannot report a device name, so the index-to-device mapping is
  established empirically with --probe, never trusted from the order text.

  Probed result, 2026-09-02: index 1 is the C922 aimed at the monitor and is
  the only one that honours 1920x1080. Index 0 is a second camera facing the
  room and caps at 1280x720. Hence --index defaults to 1, NOT the 0 the order
  assumed. Re-probe after any USB change; these indexes are not stable across
  replug.

UPLOAD LEG, read this before wondering why nothing is in Drive:
  The v1 bus (Code.gs) implements ping/time/read/append only -- there is no
  binary upload action, and this machine has no Google Drive for Desktop sync
  root. So a scheduled job has NO path into Drive today. Frames are staged
  locally and the run is reported honestly as STAGED, never as delivered.
  Wire the Drive leg by deploying the `upload` action in
  scripts/codegs_upload_action.gs, then pass --upload bus.

USAGE
  python scripts/glasses_capture.py --probe                     # map camera indexes
  python scripts/glasses_capture.py --once                      # screen grab (default)
  python scripts/glasses_capture.py --once --source camera      # opt in to the webcam
  python scripts/glasses_capture.py --loop --interval 30
  python scripts/glasses_capture.py --once --upload bus         # what the scheduler runs

SOURCE DEFAULT · screen, by Mr. Salam's ruling of 2026-09-02. Drive runs OCR over
every uploaded frame, so on-screen text becomes SEARCHABLE to every instance with
access to the Glasses Intake folder -- demonstrated that day, when a webcam frame
gave up Script Property names and a deployment URL through Drive's own OCR. A
camera aimed at a desk turns anything lying there into a grep target; a screen
grab captures only what is deliberately on the monitor. --source camera opts in.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_STAGE = REPO / "data" / "glasses_intake"
ENV_FILE = REPO / ".env"

# The C920/C922 auto-exposure ramps for roughly a second after the stream
# opens; the first frames come back dark or blown out. Discard them.
WARMUP_FRAMES = 5

# Drive folder "Glasses Intake", created 2026-09-02 inside "SFDC 24 - Claude".
DRIVE_FOLDER_ID = "1skJIwAYenlwMovtW07BsdFOi18cFchlZ"


def utc_stamp() -> str:
    """UTC ISO-8601, filename-safe. Colons are illegal in Windows filenames,
    so they become dashes -- the string still sorts lexicographically, which
    is the whole point of timestamping the name."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def load_env() -> dict:
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def grab_camera(index: int, width: int, height: int):
    """Open a camera, force MJPG at the requested size, discard warmup frames,
    return the first settled frame. Raises on any failure -- a black frame is
    a failure the caller must see, not a file to write (D-9)."""
    import cv2

    # CAP_DSHOW is the backend that actually honours MJPG + resolution on
    # Windows; the default MSMF backend silently ignores the fourcc here.
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise RuntimeError(f"camera index {index} would not open")
    try:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        frame = None
        for _ in range(WARMUP_FRAMES + 1):
            ok, f = cap.read()
            if ok:
                frame = f
            time.sleep(0.12)
        if frame is None:
            raise RuntimeError(f"camera index {index} opened but returned no frame")

        actual = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                  int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        return frame, actual
    finally:
        cap.release()


def grab_screen():
    """Native screenshot fallback. Immune to the moire and glare that a camera
    aimed at an LCD suffers, so this is the path that keeps text readable."""
    import cv2
    import numpy as np
    import mss

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])  # [1] is the primary monitor
        img = np.array(shot)[:, :, :3]  # BGRA -> BGR
    return img, (img.shape[1], img.shape[0])


def frame_stats(frame) -> dict:
    """Cheap legibility signal. A frame that is uniformly dark or blown out
    has near-zero spread -- worth failing on rather than shipping a black
    JPEG that looks like a successful capture in the folder listing."""
    import numpy as np

    gray = frame.mean(axis=2) if frame.ndim == 3 else frame
    return {
        "mean": round(float(np.mean(gray)), 1),
        "stdev": round(float(np.std(gray)), 1),
    }


def write_jpeg(frame, path: Path, quality: int) -> int:
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("cv2.imencode failed to produce a JPEG")
    path.write_bytes(buf.tobytes())
    return path.stat().st_size


def upload_via_bus(path: Path, cfg: dict) -> dict:
    """POST the frame as base64 to whichever endpoint can store it.

    Prefers GLASSES_URL -- the standalone uploader in scripts/glasses_uploader.gs.
    That exists because on 2026-09-02 the v1 bus turned out to be a CONTAINER-BOUND
    script (bound to the "Blackboard - Alpha DB" sheet), which is why it never
    appeared in the standalone project list or in Drive's script search, and so
    could not be patched. Falls back to BUS_URL for the day the bus gains the
    action from scripts/codegs_upload_action.gs.

    Either way, a gateway that does not know the action answers with an error and
    this raises -- loudly, by design. Never report a staged frame as delivered.
    """
    url, secret = cfg.get("GLASSES_URL"), cfg.get("GLASSES_SECRET")
    if not url or not secret:
        url, secret = cfg.get("BUS_URL"), cfg.get("BUS_SECRET")
    if not url or not secret:
        raise RuntimeError("no upload endpoint configured: set GLASSES_URL / "
                           "GLASSES_SECRET (or BUS_URL / BUS_SECRET) in .env (D-18)")

    payload = {
        "action": "upload",
        "secret": secret,
        "folderId": DRIVE_FOLDER_ID,
        "filename": path.name,
        "mimeType": "image/jpeg",
        "base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    # Apps Script answers 302 and urllib follows it with a GET, which is the
    # correct two-hop shape here -- same asymmetry scripts/bus.ps1 documents.
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        out = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"gateway returned non-JSON (redirect artifact?): {body[:200]}")
    if not out.get("ok"):
        raise RuntimeError(f"gateway refused the upload: {body[:300]}")
    return out


def prune_local(stage: Path, keep: int, max_age_min: int, protect: Path) -> dict:
    """Delete staged frames that are older than max_age_min OR beyond the newest
    `keep`. Either cap alone is enough to mark a frame; both default to on.

    Deliberately narrow so this can never eat something it did not create:
    only files matching glasses_*.jpg, only in the staging directory itself
    (no recursion), and never the frame this run just wrote. Probe frames are
    left alone; they are diagnostics a human asked for. 0 disables either cap.

    Age comes from the file's mtime rather than its filename. The name carries a
    UTC stamp and would work, but mtime cannot drift out of sync with reality if
    a file is ever copied or renamed by hand.
    """
    if keep <= 0 and max_age_min <= 0:
        return {"pruned": 0, "bytes_freed": 0, "note": "disabled (--keep 0 --max-age-min 0)"}

    frames = [f for f in stage.glob("glasses_*.jpg") if f.is_file()]
    frames.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    cutoff = time.time() - (max_age_min * 60) if max_age_min > 0 else None
    doomed = []
    for i, f in enumerate(frames):
        too_many = keep > 0 and i >= keep
        too_old = cutoff is not None and f.stat().st_mtime < cutoff
        if too_many or too_old:
            doomed.append(f)

    freed = 0
    removed = 0
    aged_out = 0
    for f in doomed:
        if f.resolve() == protect.resolve():
            continue
        try:
            st = f.stat()
            if cutoff is not None and st.st_mtime < cutoff:
                aged_out += 1
            f.unlink()
            freed += st.st_size
            removed += 1
        except OSError:
            # A locked or already-gone file is not worth failing the capture over.
            continue
    return {
        "pruned": removed,
        "aged_out": aged_out,
        "bytes_freed": freed,
        "remaining": len(frames) - removed,
    }


def prune_drive(cfg: dict, keep: int, max_age_min: int) -> dict:
    """Ask the uploader to trash all but the `keep` newest frames in the folder.

    Requires the 'prune' action in scripts/glasses_uploader.gs. The gateway
    TRASHES rather than destroys, so a mistake is recoverable from Drive's bin.
    Disabled by default: nothing deletes anything remote unless asked.
    """
    if keep <= 0 and max_age_min <= 0:
        return {"note": "disabled (--drive-keep 0 --drive-max-age-min 0)"}

    url, secret = cfg.get("GLASSES_URL"), cfg.get("GLASSES_SECRET")
    if not url or not secret:
        raise RuntimeError("--drive-keep needs GLASSES_URL / GLASSES_SECRET in .env")

    req = urllib.request.Request(
        url,
        data=json.dumps({
            "action": "prune",
            "secret": secret,
            "folderId": DRIVE_FOLDER_ID,
            "keep": keep,
            "maxAgeMin": max_age_min,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        out = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"gateway returned non-JSON: {body[:200]}")
    if not out.get("ok"):
        raise RuntimeError(f"gateway refused the prune: {body[:300]}")
    return out


def probe(width: int, height: int, stage: Path) -> int:
    """Capture one frame from every index that opens, so the index-to-device
    mapping is established by looking, not by guessing."""
    stage.mkdir(parents=True, exist_ok=True)
    found = 0
    for idx in range(6):
        try:
            frame, actual = grab_camera(idx, width, height)
        except RuntimeError as exc:
            print(f"index {idx}: {exc}")
            continue
        out = stage / f"probe_index{idx}_{utc_stamp()}.jpg"
        size = write_jpeg(frame, out, 90)
        print(f"index {idx}: OPENED {actual[0]}x{actual[1]} {frame_stats(frame)} "
              f"-> {out.name} ({size:,} bytes)")
        found += 1
    if not found:
        print("no camera index opened -- check the USB connection, or use --source screen")
    return 0 if found else 1


def capture_once(args, cfg) -> int:
    stage = Path(args.stage)
    if args.source == "screen":
        frame, actual = grab_screen()
        label = "screen"
    else:
        frame, actual = grab_camera(args.index, args.width, args.height)
        label = f"cam{args.index}"

    stats = frame_stats(frame)
    name = f"glasses_{label}_{utc_stamp()}.jpg"
    path = stage / name
    size = write_jpeg(frame, path, args.quality)

    record = {
        "file": str(path),
        "resolution": f"{actual[0]}x{actual[1]}",
        "bytes": size,
        "stats": stats,
        "delivery": "STAGED",
    }

    # A frame with no tonal spread is a dead capture (lens cap, sleeping
    # monitor, camera held by another process). Say so; do not pretend.
    if stats["stdev"] < 3.0:
        record["warning"] = ("near-uniform frame (stdev %.1f) -- likely a black or "
                             "blown-out capture, not a usable image" % stats["stdev"])

    if args.upload == "bus":
        try:
            record["gateway"] = upload_via_bus(path, cfg)
            record["delivery"] = "UPLOADED"
        except Exception as exc:  # noqa: BLE001 - the reason must reach the log
            record["delivery"] = "STAGED_UPLOAD_FAILED"
            record["upload_error"] = str(exc)

    # Prune AFTER the upload, never before: a frame that failed to upload is
    # still staged evidence, and deleting it first would destroy the only copy.
    record["prune_local"] = prune_local(stage, args.keep, args.max_age_min, path)
    if args.drive_keep > 0 or args.drive_max_age_min > 0:
        try:
            record["prune_drive"] = prune_drive(cfg, args.drive_keep, args.drive_max_age_min)
        except Exception as exc:  # noqa: BLE001 - report, do not fail the capture
            record["prune_drive"] = {"error": str(exc)}

    print(json.dumps(record, indent=2))
    # D-4: the gateway's reply is not proof the row landed. Read the folder
    # back before believing this. Exit code reports the LOCAL write only.
    if record["delivery"] == "STAGED_UPLOAD_FAILED":
        return 2
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Glasses Intake capture feeder")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="capture a single frame and exit (what the scheduler calls)")
    mode.add_argument("--loop", action="store_true",
                      help="capture every --interval seconds until interrupted")
    mode.add_argument("--probe", action="store_true",
                      help="capture one frame from every camera index that opens")
    # DEFAULT IS screen, BY RULING (Mr. Salam, 2026-09-02), and it is fail-safe:
    # an invocation that forgets the flag must not switch a camera on. Drive OCRs
    # every uploaded frame, so anything the lens catches becomes searchable text
    # to every instance with folder access -- a camera aimed at the desk turns
    # stray paper into a grep target. A screen grab captures only what is already
    # on the monitor, which is under deliberate control. Pass --source camera to
    # opt in explicitly.
    p.add_argument("--source", choices=["camera", "screen"], default="screen")
    p.add_argument("--index", type=int, default=1,
                   help="camera index; 1 is the monitor-facing C922 on this machine (see --probe)")
    p.add_argument("--interval", type=float, default=30.0, help="seconds between frames in --loop")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--quality", type=int, default=90)
    p.add_argument("--stage", default=str(DEFAULT_STAGE))
    p.add_argument("--upload", choices=["none", "bus"], default="none")
    # Retention. At the scheduler's 5-minute cadence a screen frame is ~600KB,
    # so 288 frames/day is ~170MB/day in BOTH places and it never stops growing.
    # Local pruning is on by default (it only ever removes this script's own
    # output); remote pruning is opt-in, because nothing should silently delete
    # from Drive. --keep 0 / --drive-keep 0 disable them.
    p.add_argument("--keep", type=int, default=288,
                   help="keep this many newest staged frames locally (0 disables)")
    p.add_argument("--max-age-min", type=int, default=60,
                   help="delete staged frames older than this many minutes (0 disables; default 60)")
    p.add_argument("--drive-keep", type=int, default=0,
                   help="trash all but this many newest frames in the Drive folder (0 disables)")
    p.add_argument("--drive-max-age-min", type=int, default=0,
                   help="trash Drive frames older than this many minutes (0 disables)")
    args = p.parse_args(argv)

    cfg = load_env()

    if args.probe:
        return probe(args.width, args.height, Path(args.stage))
    if args.loop:
        print(f"looping every {args.interval}s -- Ctrl+C to stop", file=sys.stderr)
        while True:
            try:
                capture_once(args, cfg)
            except Exception as exc:  # noqa: BLE001 - one bad frame must not end the shift
                print(json.dumps({"error": str(exc)}), file=sys.stderr)
            time.sleep(args.interval)
    return capture_once(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
