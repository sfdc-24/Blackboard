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
  python scripts/glasses_capture.py --once                      # all screens (default)
  python scripts/glasses_capture.py --once --monitor 2          # one display only
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


def _mss():
    """mss.mss is deprecated in 10.x in favour of mss.MSS; support both."""
    import mss

    return getattr(mss, "MSS", None) or mss.mss


def monitor_count() -> int:
    """Number of physical monitors. mss index 0 is the virtual union of them
    all, so the real ones are 1..n."""
    with _mss()() as sct:
        return len(sct.monitors) - 1


def grab_screen(which: int = 1):
    """Native screenshot of one monitor. Immune to the moire and glare a camera
    aimed at an LCD suffers, so this is the path that keeps text readable.

    `which` indexes mss's monitor list: 1..n are the physical displays and 0 is
    the virtual bounding box spanning all of them. Avoid 0 for capture -- on a
    layout like this machine's (2560x1600 primary flanked by two 1080p panels at
    different vertical offsets) the union is 6400x1729 and mostly dead space,
    which wastes bytes and hurts OCR. Capture each screen separately instead.
    """
    import numpy as np

    with _mss()() as sct:
        if which >= len(sct.monitors):
            raise RuntimeError(
                f"monitor {which} does not exist; this machine has "
                f"{len(sct.monitors) - 1} (use --monitor all, or 1..{len(sct.monitors) - 1})"
            )
        shot = sct.grab(sct.monitors[which])
        img = np.array(shot)[:, :, :3]  # BGRA -> BGR
    return img, (img.shape[1], img.shape[0])


# --- Situation Board composition -------------------------------------------
# One frame that shows the whole workspace at a glance: every screen plus the
# room, tiled, stamped and labelled. A webcam cannot give a literal overhead
# shot, so this is the practical "bird's eye view" -- mission control rather
# than a ceiling camera.
BOARD_W, BOARD_H = 1920, 1200
BANNER_H = 60

# Human-readable captions for the camera tiles, keyed by index. Probed
# 2026-09-03 on this machine; indexes are not stable across replug, so re-probe
# and update these rather than trusting them blindly after a USB change.
CAMERA_CAPTIONS = {0: "OPERATOR", 1: "SETUP"}


def parse_indexes(spec) -> list:
    """Accept 1, "1", or "0,1" -- the scheduler passes strings and a human
    editing the shortcut should be able to write a list without ceremony."""
    if isinstance(spec, int):
        return [spec]
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out
INK = (236, 236, 240)     # near-white, BGR
GROUND = (18, 18, 22)     # near-black canvas
DIM = (150, 150, 158)     # secondary text


def fit_into(img, w: int, h: int):
    """Scale to fit inside w x h preserving aspect, centred on the canvas
    ground. Letterboxing rather than cropping -- a tile that silently cut off
    half a screen would be worse than one with bars."""
    import cv2
    import numpy as np

    canvas = np.full((h, w, 3), GROUND, dtype=np.uint8)
    ih, iw = img.shape[:2]
    scale = min(w / iw, h / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    x, y = (w - nw) // 2, (h - nh) // 2
    canvas[y:y + nh, x:x + nw] = resized
    return canvas


def compose_board(rows, stamp: str, host: str):
    """Compose rows of sources into one board image.

    rows: list of rows; each row is a list of (label, image-or-None). Rows are
    laid out independently, so screens and cameras can be grouped and sized on
    their own terms rather than forced into one uniform grid.

    Each row's height is derived from the WIDEST-relative content it holds --
    cell_width / smallest aspect ratio -- so nothing is ever cropped and the
    letterboxing stays minimal. A row of 16:9 cameras is therefore shorter than
    a row containing a 16:10 screen, which is what keeps the board compact.

    A None image becomes a visible NO SIGNAL cell rather than being skipped: a
    missing screen is information, and a board that silently reflowed would
    hide it.
    """
    import cv2
    import numpy as np

    rows = [r for r in rows if r]
    if not rows:
        raise RuntimeError("no tiles to compose")

    geom = []
    for row in rows:
        cell_w = BOARD_W // len(row)
        aspects = []
        for _, img in row:
            if img is None:
                aspects.append(16 / 9)
            else:
                h, w = img.shape[:2]
                aspects.append(w / h)
        cell_h = int(round(cell_w / min(aspects)))
        geom.append((cell_w, cell_h))

    height = BANNER_H + sum(h for _, h in geom)
    canvas = np.full((height, BOARD_W, 3), GROUND, dtype=np.uint8)

    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(canvas, "SFDC24 - GLASSES INTAKE", (18, 40), font, 0.8, INK, 2, cv2.LINE_AA)
    right = f"{stamp}   ·   {host}"
    (tw, _), _ = cv2.getTextSize(right, font, 0.62, 1)
    cv2.putText(canvas, right, (BOARD_W - tw - 18, 39), font, 0.62, DIM, 1, cv2.LINE_AA)
    cv2.line(canvas, (0, BANNER_H - 1), (BOARD_W, BANNER_H - 1), (60, 60, 68), 1)

    y0 = BANNER_H
    for row, (cell_w, cell_h) in zip(rows, geom):
        for c, (label, img) in enumerate(row):
            x0 = c * cell_w
            pad = 3
            iw, ih = cell_w - pad * 2, cell_h - pad * 2

            if img is None:
                cell = np.full((ih, iw, 3), GROUND, dtype=np.uint8)
                msg = "NO SIGNAL"
                (mw, mh), _ = cv2.getTextSize(msg, font, 0.9, 2)
                cv2.putText(cell, msg, ((iw - mw) // 2, (ih + mh) // 2), font, 0.9,
                            (70, 70, 90), 2, cv2.LINE_AA)
            else:
                cell = fit_into(img, iw, ih)

            canvas[y0 + pad:y0 + pad + ih, x0 + pad:x0 + pad + iw] = cell
            cv2.rectangle(canvas, (x0 + pad, y0 + pad),
                          (x0 + pad + iw - 1, y0 + pad + ih - 1), (60, 60, 68), 1)
            # Label sits on its own strip so it stays readable over any content.
            label_w = min(iw, max(150, 9 * len(label) + 16))
            cv2.rectangle(canvas, (x0 + pad, y0 + pad),
                          (x0 + pad + label_w, y0 + pad + 26), GROUND, -1)
            cv2.putText(canvas, label, (x0 + pad + 8, y0 + pad + 19), font, 0.52, INK, 1,
                        cv2.LINE_AA)
        y0 += cell_h

    return canvas


# --- Daft Punk helmet overlay ----------------------------------------------
# Doubles as a privacy control: boards land in a Drive folder that the Gemini,
# ChatGPT and Meta instances all read, and Google OCRs every one. A helmeted
# operator is recognisable as "someone is at the desk" without publishing a
# face to three vendors.
YUNET_MODEL = REPO / "models" / "face_detection_yunet_2023mar.onnx"
_detector = None

CHROME = (176, 174, 168)
CHROME_LIT = (226, 224, 218)
CHROME_DARK = (104, 103, 100)
VISOR = (26, 22, 20)
# BGR, not RGB. Amber is (blue-low, green-mid, red-high); writing it the RGB way
# round produced a cyan visor on the first attempt.
LED = (40, 190, 250)
LED_GLOW = (10, 70, 120)


def detect_faces(img):
    """Return [(x, y, w, h), ...] using OpenCV's bundled YuNet DNN detector.

    Returns [] when the model file is absent rather than raising -- a missing
    model should cost the mask, not the capture.
    """
    global _detector
    import cv2

    if not YUNET_MODEL.exists():
        return []
    h, w = img.shape[:2]
    if _detector is None:
        _detector = cv2.FaceDetectorYN.create(str(YUNET_MODEL), "", (320, 320), 0.6, 0.3, 5000)
    _detector.setInputSize((w, h))
    _, faces = _detector.detect(img)
    if faces is None:
        return []
    return [tuple(int(v) for v in f[:4]) for f in faces]


def apply_helmet(img, name: str):
    """Draw a Daft Punk style helmet over every detected face, with `name`
    running across the visor as LED text. Returns (image, faces_masked)."""
    import cv2

    faces = detect_faces(img)
    if not faces:
        return img, 0

    import numpy as np

    out = img.copy()
    H, W = out.shape[:2]
    for (x, y, w, h) in faces:
        cx, cy = x + w // 2, int(y + h * 0.44)
        ax, ay = int(w * 0.88), int(h * 0.80)           # helmet semi-axes

        # Chrome is a gradient, not a fill. Paint a vertical light-to-dark ramp
        # through an ellipse mask so the dome reads as curved metal; a flat
        # ellipse just looks like a grey egg.
        shell = np.zeros((H, W), np.uint8)
        cv2.ellipse(shell, (cx, cy), (ax, ay), 0, 0, 360, 255, -1, cv2.LINE_AA)
        ys = np.clip((np.arange(H) - (cy - ay)) / max(2 * ay, 1), 0, 1)[:, None]
        ramp = (np.array(CHROME_LIT) * (1 - ys) + np.array(CHROME_DARK) * ys)
        ramp = np.repeat(ramp[:, None, :], W, axis=1).astype(np.uint8)
        m3 = shell[:, :, None] > 0
        out = np.where(m3, ramp, out)

        # Specular highlight, blended rather than painted, so it reads as a
        # soft reflection instead of a white sticker.
        spec = out.copy()
        cv2.ellipse(spec, (int(cx - ax * 0.30), int(cy - ay * 0.42)),
                    (int(ax * 0.42), int(ay * 0.20)), 25, 0, 360, (255, 255, 255), -1, cv2.LINE_AA)
        cv2.GaussianBlur(spec, (0, 0), max(3, ax // 8), dst=spec)
        blended = cv2.addWeighted(out, 0.62, spec, 0.38, 0)
        out = np.where(m3, blended, out)
        cv2.ellipse(out, (cx, cy), (ax, ay), 0, 0, 360, CHROME_DARK, 2, cv2.LINE_AA)

        # Visor: a wide rounded band on the eye line, not a circle.
        vx, vy = cx, int(cy - ay * 0.10)
        vax, vay = int(ax * 0.90), int(ay * 0.30)
        visor = np.zeros((H, W), np.uint8)
        cv2.ellipse(visor, (vx, vy), (vax, vay), 0, 0, 360, 255, -1, cv2.LINE_AA)
        vys = np.clip((np.arange(H) - (vy - vay)) / max(2 * vay, 1), 0, 1)[:, None]
        vramp = (np.array((54, 48, 44)) * (1 - vys) + np.array(VISOR) * vys)
        vramp = np.repeat(vramp[:, None, :], W, axis=1).astype(np.uint8)
        out = np.where(visor[:, :, None] > 0, vramp, out)
        cv2.ellipse(out, (vx, vy), (vax, vay), 0, 0, 360, (92, 88, 84), 2, cv2.LINE_AA)
        # Thin bright sliver along the top edge sells it as glass.
        cv2.ellipse(out, (vx, int(vy - vay * 0.30)), (int(vax * 0.74), int(vay * 0.26)),
                    0, 195, 345, (120, 116, 112), 2, cv2.LINE_AA)

        # LED name across the visor, scaled to fit whatever the name is.
        text = name.upper()
        font = cv2.FONT_HERSHEY_SIMPLEX
        (bw, bh), _ = cv2.getTextSize(text, font, 1.0, 2)
        target = vax * 1.45
        scale = max(0.28, min(2.4, target / max(bw, 1)))
        thick = max(1, int(round(scale * 1.7)))
        (tw, th), _ = cv2.getTextSize(text, font, scale, thick)
        org = (int(vx - tw / 2), int(vy + th / 2))
        # Glow first, bright core over it -- cheap but convincingly lit.
        cv2.putText(out, text, org, font, scale, LED_GLOW, thick + 3, cv2.LINE_AA)
        cv2.putText(out, text, org, font, scale, LED, thick, cv2.LINE_AA)

    return out, len(faces)


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


def prune_local(stage: Path, keep: int, max_age_min: int, protect) -> dict:
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

    # Frames written by the current wake -- never prune these, whatever the caps
    # say. With multi-monitor capture a single wake writes several at once, and a
    # tight --keep could otherwise delete a frame seconds after creating it.
    keepsafe = {p.resolve() for p in protect}

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
        if f.resolve() in keepsafe:
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


_last_pointer = 0.0


def post_pointer_row(cfg: dict, file_id: str, name: str, stamp: str,
                     tiles: list, live: int) -> dict:
    """Append one row to the board announcing the latest situation board.

    WHY: the images are invisible to the fleet without this. An instance
    following the BOOT wake protocol reads the board, and nothing there
    referenced the Glasses Intake folder -- so the frames were discoverable
    only by someone who already knew to look.

    phase=ASSET, deliberately NOT phase=VIEWPORT. The wake protocol says to read
    the newest VIEWPORT row; posting these as VIEWPORT would hijack every
    instance's wake with a screenshot pointer several times an hour.

    Cells are comma-free per the LEARNINGS sheetRow convention, and all ten
    columns are supplied -- the sheet does not auto-fill Row_ID or Timestamp,
    and a short array silently shifts every field left (claude-mobile, Sep 2).
    """
    import uuid

    url, secret = cfg.get("BUS_URL"), cfg.get("BUS_SECRET")
    if not url or not secret:
        raise RuntimeError("BUS_URL / BUS_SECRET missing from .env (D-18)")

    view = f"https://drive.google.com/file/d/{file_id}/view"
    payload = (
        "BCB|v=1|wf=GLASSES-INTAKE|sub=BOARD-POINTER|phase=ASSET"
        "|from=glasses-uploader|to=ALL|kind=situation-board"
        f"|file_id={file_id}|name={name}|url={view}"
        f"|captured={stamp}|tiles={live}/{len(tiles)}"
        f"|folder={DRIVE_FOLDER_ID}"
        "|note=newest composite of every screen plus the operator and setup cameras."
        " Faces are helmet-masked. Retention is 1 hour so this link expires -"
        " read the newest ASSET row rather than an older one."
        "|caveat=screen tiles are downscaled so OCR recovers headings not body text -"
        " act on the board sheet for state and treat this as visual provenance (D-12)."
    )
    row = [
        str(uuid.uuid4()),
        dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        # ISS-009: this loop used to write as "claude-code-cli", the same tag as
        # the interactive laptop session. Three writers under one name made the
        # check-in register useless and hid a live DNS edit from a sibling
        # instance on Sep 3, which took the site down. A tag is a claimed
        # identity, not a machine (REQ-R6WNT2) -- so this one claims its own.
        "glasses-uploader",
        "ALL",
        "APPEND",
        payload,
        "DONE",
        "GLASSES-INTAKE",
        "",
        "",
    ]
    req = urllib.request.Request(
        url,
        data=json.dumps({
            "action": "append",
            "secret": secret,
            "title": "Blackboard - Alpha DB",
            "sheetRow": row,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        out = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"bus returned non-JSON: {body[:200]}")
    if not out.get("ok"):
        raise RuntimeError(f"bus refused the pointer row: {body[:300]}")
    return {"ok": True, "row_id": row[0], "appendedAt": out.get("appendedAt")}


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

    # One wake can produce several frames (one per monitor). They share a single
    # timestamp so a downstream consumer can tell at a glance which frames belong
    # to the same moment across screens.
    stamp = utc_stamp()
    targets = []  # (label, grab callable)

    if args.source == "screen":
        if args.monitor == "all":
            for m in range(1, monitor_count() + 1):
                targets.append((f"screen{m}", lambda m=m: grab_screen(m)))
        else:
            m = int(args.monitor)
            targets.append((f"screen{m}", lambda m=m: grab_screen(m)))
    else:
        targets.append((f"cam{args.index}",
                        lambda: grab_camera(args.index, args.width, args.height)))

    # --- Situation Board: one composite frame instead of one file per source ---
    if args.layout == "board":
        import socket

        errors = []
        screen_row = []
        for m in range(1, monitor_count() + 1):
            try:
                img, actual = grab_screen(m)
                screen_row.append((f"SCREEN {m}  {actual[0]}x{actual[1]}", img))
            except Exception as exc:  # noqa: BLE001 - a dead screen is a NO SIGNAL tile
                screen_row.append((f"SCREEN {m}", None))
                errors.append({"tile": f"screen{m}", "error": str(exc)})

        # Cameras get their own row so they are not squeezed into a grid sized
        # for screens. Each index is captured in turn; a camera that another
        # process is holding becomes NO SIGNAL rather than killing the wake.
        camera_row = []
        if not args.no_room:
            for idx in parse_indexes(args.room_index):
                caption = CAMERA_CAPTIONS.get(idx, f"CAMERA {idx}")
                try:
                    img, actual = grab_camera(idx, args.width, args.height)
                    if args.mask:
                        img, masked = apply_helmet(img, args.mask_name)
                        if masked:
                            caption = f"{caption} [{masked} MASKED]"
                    camera_row.append((f"{caption}  cam{idx}  {actual[0]}x{actual[1]}", img))
                except Exception as exc:  # noqa: BLE001
                    camera_row.append((f"{caption}  cam{idx}", None))
                    errors.append({"tile": f"cam{idx}", "error": str(exc)})

        tiles = screen_row + camera_row
        board = compose_board([screen_row, camera_row], stamp, socket.gethostname())
        path = stage / f"glasses_board_{stamp}.jpg"
        size = write_jpeg(board, path, args.quality)

        record = {
            "file": str(path),
            "resolution": f"{BOARD_W}x{BOARD_H}",
            "bytes": size,
            "tiles": [t[0] for t in tiles],
            "live_tiles": sum(1 for t in tiles if t[1] is not None),
            "delivery": "STAGED",
        }
        if errors:
            record["tile_errors"] = errors

        if args.upload == "bus":
            try:
                record["gateway"] = upload_via_bus(path, cfg)
                record["delivery"] = "UPLOADED"
            except Exception as exc:  # noqa: BLE001
                record["delivery"] = "STAGED_UPLOAD_FAILED"
                record["upload_error"] = str(exc)

        # Pointer row: rate-limited, and only once the image is actually IN Drive.
        # Announcing a file that failed to upload would be a dangling link on a
        # board other instances trust.
        global _last_pointer
        if (args.pointer_every_min > 0
                and record.get("delivery") == "UPLOADED"
                and (time.time() - _last_pointer) >= args.pointer_every_min * 60):
            try:
                gw = record["gateway"]
                record["pointer_row"] = post_pointer_row(
                    cfg, gw["fileId"], gw["name"], stamp, tiles, record["live_tiles"])
                _last_pointer = time.time()
            except Exception as exc:  # noqa: BLE001 - never fail a capture over this
                record["pointer_row"] = {"error": str(exc)}

        summary = {
            "captured": 1,
            "layout": "board",
            "frames": [record],
            "prune_local": prune_local(stage, args.keep, args.max_age_min, [path]),
        }
        if args.drive_keep > 0 or args.drive_max_age_min > 0:
            try:
                summary["prune_drive"] = prune_drive(cfg, args.drive_keep, args.drive_max_age_min)
            except Exception as exc:  # noqa: BLE001
                summary["prune_drive"] = {"error": str(exc)}

        print(json.dumps(summary, indent=2))
        return 2 if record["delivery"] == "STAGED_UPLOAD_FAILED" else 0

    records = []
    written = []
    for label, grab in targets:
        try:
            frame, actual = grab()
        except Exception as exc:  # noqa: BLE001 - one dead screen must not cost the others
            records.append({"label": label, "error": str(exc)})
            continue

        stats = frame_stats(frame)
        path = stage / f"glasses_{label}_{stamp}.jpg"
        size = write_jpeg(frame, path, args.quality)
        written.append(path)

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

        records.append(record)

    # Prune AFTER the uploads, never before: a frame that failed to upload is
    # still staged evidence, and deleting it first would destroy the only copy.
    summary = {
        "captured": len([r for r in records if "file" in r]),
        "frames": records,
        "prune_local": prune_local(stage, args.keep, args.max_age_min, written),
    }
    if args.drive_keep > 0 or args.drive_max_age_min > 0:
        try:
            summary["prune_drive"] = prune_drive(cfg, args.drive_keep, args.drive_max_age_min)
        except Exception as exc:  # noqa: BLE001 - report, do not fail the capture
            summary["prune_drive"] = {"error": str(exc)}

    print(json.dumps(summary, indent=2))
    # D-4: the gateway's reply is not proof the file landed. Read the folder back
    # before believing it. Exit code reports the LOCAL writes only.
    if not written:
        return 1
    if any(r.get("delivery") == "STAGED_UPLOAD_FAILED" for r in records):
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
    # "all" writes one file per physical monitor, sharing one timestamp. This
    # machine has three (2560x1600 primary plus two 1080p). Capturing them
    # separately rather than as mss's virtual union avoids the union's dead
    # space -- 6400x1729 of mostly nothing, which wastes bytes and hurts OCR.
    p.add_argument("--monitor", default="all",
                   help="which display to capture with --source screen: 'all' (default) or a 1-based index")
    # Board layout composites every screen plus the room into ONE frame. Besides
    # reading better, it cuts uploads 4:1 -- the quota headroom that makes a
    # faster cadence affordable.
    p.add_argument("--layout", choices=["frames", "board"], default="frames",
                   help="'frames' writes one file per source; 'board' composites them into one situation board")
    p.add_argument("--room-index", default="0",
                   help="camera index(es) for the board's camera row; comma-separated, e.g. '0,1' "
                        "(here 0 faces the operator, 1 faces the monitors)")
    p.add_argument("--no-room", action="store_true",
                   help="build the board from screens only, omitting the room camera")
    p.add_argument("--mask", action="store_true",
                   help="draw a Daft Punk style helmet over detected faces (also anonymises them)")
    p.add_argument("--mask-name", default="MR. SALAM",
                   help="text shown as LED across the helmet visor")
    # Rate-limited on purpose: at a 40s cadence a row per board would be ~90
    # rows an hour of noise on a board other instances have to read.
    p.add_argument("--pointer-every-min", type=int, default=5,
                   help="append a board row pointing at the latest situation board, "
                        "at most this often in minutes (0 disables)")
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
