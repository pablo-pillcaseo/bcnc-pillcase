#!/usr/bin/env python3
"""Print the lid camera's fiducial markers on the 4B-2054A / Arkscan 2054A.

Each label holds a grid of ArUco markers (DICT_4X4_50, the dictionary
bCNC/LidVision.py looks for), each inside the white border LidVision uses as its
white reference. Cut them apart along the dotted lines and stick them flat
around the fixture, at least one where the camera sees it with the lid in place.
More markers make the measurement more accurate. Their positions do not matter
and do not need measuring.

The printer takes TSPL, sent raw through CUPS (`lp -o raw`), so the queue's own
driver is bypassed and a dot is a dot: at 203 dpi a 30 mm marker is 240 dots,
an exact 40 dots per marker cell.

Check the printed size with calipers (the black square, edge to edge, both
ways) and put it in Lid Camera… > Marker size. A 50 mm ruler is printed on
every label as well.

    python3 tools/print_fiducials.py --dry-run           # PNG only, to look at
    python3 tools/print_fiducials.py --queue 4B-2054A-2  # print ids 0-5
    python3 tools/print_fiducials.py --first-id 6        # the next six
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bCNC"))

import cv2 as cv  # noqa: E402
import numpy as np  # noqa: E402

import LidVision  # noqa: E402

DPI = 203
DOTS_PER_MM = DPI / 25.4


def dots(mm):
    return int(round(mm * DOTS_PER_MM))


def marker_side_dots(marker_mm):
    """The marker's side in dots, rounded to whole dots per cell (4x4 + border = 6)."""
    return max(6, int(round(marker_mm * DOTS_PER_MM / 6.0)) * 6)


def render_label(first_id, label_mm, marker_mm, cols, rows):
    """The label as a white image with black markers (uint8, 0 = black)."""
    w, h = dots(label_mm[0]), dots(label_mm[1])
    img = np.full((h, w), 255, np.uint8)
    side = marker_side_dots(marker_mm)
    border = int(round(side * LidVision.QUIET_ZONE))
    tile = side + 2 * border
    ruler_h = dots(10)
    gx = (w - cols * tile) // (cols + 1)
    gy = (h - ruler_h - rows * tile) // (rows + 1)
    if gx < 0 or gy < 0:
        raise SystemExit("%d x %d markers of %g mm do not fit a %g x %g mm label"
                         % (cols, rows, marker_mm, label_mm[0], label_mm[1]))
    actual_mm = side / DOTS_PER_MM
    ids = []
    for r in range(rows):
        for c in range(cols):
            mid = first_id + r * cols + c
            if mid >= 50:
                break
            x = gx + c * (tile + gx)
            y = gy + r * (tile + gy)
            img[y + border:y + border + side, x + border:x + border + side] = \
                LidVision.marker_image(mid, side)
            # Dotted cut line around the tile, outside its white border.
            for px in range(x - 4, x + tile + 4, 8):
                for py in (y - 4, y + tile + 3):
                    img[max(py, 0):py + 1, max(px, 0):px + 3] = 0
            for py in range(y - 4, y + tile + 4, 8):
                for px in (x - 4, x + tile + 3):
                    img[max(py, 0):py + 3, max(px, 0):px + 1] = 0
            # Caption in the bottom border. It is a few percent of the white
            # reference ring, which LidVision takes the median of.
            cv.putText(img, "ID %d  %.2f mm" % (mid, actual_mm),
                       (x + border, y + border + side + int(border * 0.7)),
                       cv.FONT_HERSHEY_SIMPLEX, 0.45, 0, 1, cv.LINE_AA)
            ids.append(mid)

    # A 50 mm ruler to check the printer's scale across the label. Check it
    # along the feed direction by measuring the black squares both ways.
    y0 = h - ruler_h + dots(3)
    x0 = dots(8)
    img[y0:y0 + 2, x0:x0 + dots(50)] = 0
    for mm in range(0, 51, 5):
        tick = dots(3) if mm % 10 == 0 else dots(1.5)
        img[y0 - tick:y0, x0 + dots(mm):x0 + dots(mm) + 2] = 0
    cv.putText(img, "50 mm", (x0 + dots(52), y0 + 4), cv.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1,
               cv.LINE_AA)
    _, img = cv.threshold(img, 128, 255, cv.THRESH_BINARY)
    return img, ids, actual_mm


def tspl(img, label_mm, gap_mm, copies=1):
    """A TSPL job printing `img` (0 = black) full-label."""
    h, w = img.shape
    width_bytes = (w + 7) // 8
    padded = np.full((h, width_bytes * 8), 255, np.uint8)
    padded[:, :w] = img
    # TSPL BITMAP: one bit per dot, MSB first, 0 prints black.
    bits = np.packbits(padded >= 128, axis=1)
    head = ("SIZE %.1f mm, %.1f mm\r\nGAP %.1f mm, 0 mm\r\nDIRECTION 1\r\nREFERENCE 0,0\r\n"
            "DENSITY 10\r\nCLS\r\n" % (label_mm[0], label_mm[1], gap_mm)).encode()
    bitmap = b"BITMAP 0,0,%d,%d,0," % (width_bytes, h) + bits.tobytes() + b"\r\n"
    return head + bitmap + b"PRINT 1,%d\r\n" % copies


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--queue", default="4B-2054A-2", help="CUPS queue of the label printer")
    ap.add_argument("--label", default="101.6x152.4", help="label size in mm, W x H (4x6 in)")
    ap.add_argument("--gap", type=float, default=3.0, help="gap between labels, mm")
    ap.add_argument("--marker-mm", type=float, default=LidVision.DEFAULT_MARKER_MM)
    ap.add_argument("--first-id", type=int, default=0)
    ap.add_argument("--cols", type=int, default=2)
    ap.add_argument("--rows", type=int, default=3)
    ap.add_argument("--labels", type=int, default=1, help="labels to print, next ids on each")
    ap.add_argument("--out", default=None, help="folder for the PNG previews")
    ap.add_argument("--dry-run", action="store_true", help="write PNGs, do not print")
    a = ap.parse_args()

    label_mm = tuple(float(v) for v in a.label.lower().split("x"))
    out = a.out or os.getcwd()
    first = a.first_id
    for _n in range(a.labels):
        img, ids, actual = render_label(first, label_mm, a.marker_mm, a.cols, a.rows)
        if not ids:
            break
        png = os.path.join(out, "fiducials_%d-%d.png" % (ids[0], ids[-1]))
        cv.imwrite(png, img)
        print("label: markers %s, %.2f mm each -> %s" % (ids, actual, png))
        if not a.dry_run:
            job = tspl(img, label_mm, a.gap)
            subprocess.run(["lp", "-d", a.queue, "-o", "raw", "-t", "LidCam fiducials"],
                           input=job, check=True)
        first = ids[-1] + 1


if __name__ == "__main__":
    main()
