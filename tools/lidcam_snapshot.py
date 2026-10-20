#!/usr/bin/env python3
"""Take one photo with the lid camera and run the lid vision on it, no mill or
ShipHero needed.

Every lid (its physical outline, bCNC/lid_outlines.json) in every thumbnail colour is a
candidate, so this answers "what does the camera think is on the bed" and
shows what it measured. Close bCNC first, or at least its BLTouch port.

    python tools/lidcam_snapshot.py                     # auto-finds the board
    python tools/lidcam_snapshot.py --port /dev/ttyACM1 --out /tmp/lidcam
    python tools/lidcam_snapshot.py --image photo.jpg   # re-run on a saved photo
    python tools/lidcam_snapshot.py --empty-bed         # save the empty-bed photo

Writes <out>/photo.jpg (as taken), annotated.jpg (markers in orange, the lid
outline in green) and topdown.jpg (the rectified, white-balanced view).
"""

import argparse
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bCNC"))

import cv2 as cv  # noqa: E402

import BltCamera  # noqa: E402
import LidVision  # noqa: E402

THUMBS = os.path.join(HERE, "..", "bCNC", "color_thumbnails")
OUTLINES = os.path.join(HERE, "..", "bCNC", "lid_outlines.json")


def find_port():
    hits = sorted(glob.glob("/dev/serial/by-id/*Espressif*") + glob.glob("/dev/serial/by-id/*XIAO*"))
    if not hits:
        raise SystemExit("No XIAO found under /dev/serial/by-id - pass --port.")
    return hits[0]


def take_photo(port_name):
    import serial
    port = serial.Serial(port_name, 9600, timeout=0.05)
    try:
        time.sleep(0.3)
        port.reset_input_buffer()
        version, has_camera = BltCamera.identify_version(port, 2.0)
        if not has_camera:
            raise SystemExit("The board did not answer as camera firmware (BLTCAM).")
        rec = BltCamera.apply_saved_exposure(port, version, BltCamera.default_exposure_path())
        if rec:
            print("exposure fixed: aec=%s agc=%s wb=%s" % (rec["aec"], rec["agc"], rec["wb"]))
        port.reset_input_buffer()
        t = time.time()
        shot = BltCamera.capture(port, first=True)
        print("photo %dx%d, %d bytes in %.2fs" % (shot.width, shot.height, len(shot.jpeg),
                                                  time.time() - t))
        return shot.jpeg
    finally:
        port.close()


def candidates():
    # Physical lid outlines, not engraving frames: the camera sees the lid.
    lids = json.load(open(OUTLINES))["lids"]
    colours = sorted(os.path.splitext(f)[0] for f in os.listdir(THUMBS) if f.endswith(".png"))
    out = []
    for d in lids.values():
        for c in colours:
            out.append({"key": d["name"] + "|" + c, "size": (d["width"], d["length"]),
                        "colour_key": c, "lid": d["name"]})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", help="the XIAO's serial port (default: auto)")
    ap.add_argument("--image", help="run on this JPEG instead of taking a photo")
    ap.add_argument("--out", default=".", help="folder for the output images")
    ap.add_argument("--marker-mm", type=float, default=LidVision.DEFAULT_MARKER_MM)
    ap.add_argument("--background", help="empty-bed record saved with --empty-bed")
    ap.add_argument("--empty-bed", action="store_true",
                    help="this photo is of the empty bed: save it as the background")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    if a.image:
        jpeg = open(a.image, "rb").read()
    else:
        jpeg = take_photo(a.port or find_port())
        open(os.path.join(a.out, "photo.jpg"), "wb").write(jpeg)

    cache = {}

    def ref(key):
        if key not in cache:
            cache[key] = LidVision.colour_of_thumbnail(os.path.join(THUMBS, key + ".png"))
        return cache[key]

    bg = None
    bg_base = a.background or os.path.join(a.out, "empty-bed")
    if not a.empty_bed and os.path.exists(bg_base + ".json"):
        bg = json.load(open(bg_base + ".json"))
        bg["image"] = cv.imread(bg_base + ".png")
        print("using empty-bed photo", bg_base + ".png")

    res = LidVision.identify(jpeg, [] if a.empty_bed else candidates(), ref, a.marker_mm,
                             background=bg)
    cv.imwrite(os.path.join(a.out, "annotated.jpg"), res.annotated)
    if res.rectified is not None:
        cv.imwrite(os.path.join(a.out, "topdown.jpg"), res.rectified)

    markers = sorted(res.plane.layout) if res.plane else []
    print("markers seen:", markers or "none",
          "(fit %.2f mm rms)" % res.plane.rms_mm if res.plane else "")
    for w in res.warnings:
        print("warning:", w)

    if a.empty_bed:
        rec = LidVision.background_record(res, a.marker_mm)
        if rec is None:
            raise SystemExit("No markers in view: nothing saved.")
        cv.imwrite(bg_base + ".png", rec["image"])
        json.dump({k: v for k, v in rec.items() if k != "image"}, open(bg_base + ".json", "w"))
        print("saved", bg_base + ".png")
        return

    if res.lid:
        print("lid: %s, found by %s" % (LidVision.describe_size(res.lid), res.lid.method))
    if res.colour_lab is not None:
        print("colour (Lab median): L %.0f  a %.0f  b %.0f" % tuple(res.colour_lab))
    print("best matches:")
    for m in res.matches[:6]:
        print("  %5.1f%%  %-28s %-24s size %s  colour %s" % (
            m.probability * 100, m.first["lid"], m.first["colour_key"],
            "-" if m.size_error is None else "%.1fσ" % m.size_error,
            "-" if m.colour_error is None else "%.1fσ" % m.colour_error))
    print("  %5.1f%%  none of these" % (res.none_probability * 100))
    print("images in", os.path.abspath(a.out))


if __name__ == "__main__":
    main()
