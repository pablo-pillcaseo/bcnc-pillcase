#!/usr/bin/env python3
"""Physical lid outlines, for the lid camera, from "Pill Case Templates.zip".

The lid camera measures the lid itself. The configured lid sizes
(lid_dimensions.json, and each machine's saved lid settings) are engraving
frames: for most lids the same as the lid, but the Mission lids' frames are
deliberately shortened to put the text above the thumb grip (44 x 50 for a
44.45 x 82.55 mm lid). Matching a photo against a frame fails for those.

The templates zip holds 1:1 top-down drawings of every part, generated from the
STEP models; each *_Lid.svg carries a caption with its outer size, "W × H mm".
This reads those captions (stdlib only) and writes bCNC/lid_outlines.json,
keyed by the same product codes as the lid settings:

    python3 tools/extract_lid_outlines.py ~/Downloads/"Pill Case Templates.zip"

Nano and MagNano are out of scope, as in extract_lid_dimensions.py.
"""

import argparse
import json
import os
import re
import sys
import zipfile

# Template folder prefix -> (product code, title), as extract_lid_dimensions.py.
PRODUCT_CODES = {
    "SD3ULS-":   ("MPC",      "Mission Pill Case"),
    "SD3ULSM-":  ("MVC",      "Mission Vitamin Case"),
    "SD7ULS-":   ("WXSPC",    "Weekly XS Pill Case"),
    "SD7UL-":    ("WPC",      "Weekly Pill Case"),
    "SD7ULM-XL": ("WVXC",     "Weekly Vitamin XL Case"),
    "SD7ULM-":   ("WVC",      "Weekly Vitamin Case"),
    "WV2XL-":    ("WV2XC",    "Weekly 2XL Case"),
    "SD7-MAGS-": ("AMPM-PC",  "Weekly AM-PM Pill Case"),
    "SD7-MAG-":  ("AMPM-VC",  "Weekly AM-PM Vitamin Case"),
    "WAPVXL-":   ("AMPM-VXL", "Weekly AM-PM Vitamin XL Case"),
}

_CAPTION = re.compile(r"<text[^>]*>\s*([\d.]+)\s*[×x]\s*([\d.]+)\s*mm\s*<", re.I)


def product_of(folder):
    # Longest prefix first: SD7ULM-XL before SD7ULM-, SD7-MAGS- before SD7-MAG-.
    for prefix in sorted(PRODUCT_CODES, key=len, reverse=True):
        if folder.startswith(prefix):
            return PRODUCT_CODES[prefix]
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("zip", help="Pill Case Templates.zip")
    ap.add_argument("-o", "--out", default=os.path.join(os.path.dirname(__file__), "..",
                                                       "bCNC", "lid_outlines.json"))
    a = ap.parse_args()

    lids = {}
    with zipfile.ZipFile(a.zip) as z:
        for name in sorted(z.namelist()):
            if not name.endswith("_Lid.svg"):
                continue
            folder = name.split("/")[-2]
            product = product_of(folder)
            if product is None:
                print("skipped (out of scope):", folder)
                continue
            m = _CAPTION.search(z.read(name).decode("utf-8", "replace"))
            if not m:
                sys.exit("no size caption in %s" % name)
            w, h = sorted((float(m.group(1)), float(m.group(2))))
            code, title = product
            lids[code] = {"name": title, "width": w, "length": h,
                          "template": name.split("/", 1)[-1]}
            print("%-9s %-30s %7.2f x %7.2f mm" % (code, title, w, h))

    missing = sorted({c for c, _t in PRODUCT_CODES.values()} - set(lids))
    if missing:
        sys.exit("no template for: %s" % ", ".join(missing))
    with open(a.out, "w") as f:
        json.dump({
            "about": "Physical lid outlines (short side x long side, mm) for the lid camera, "
                     "from the OUTLINE of each *_Lid drawing in Pill Case Templates.zip. "
                     "Not engraving frames: see lid_dimensions.json for those. "
                     "Regenerate with tools/extract_lid_outlines.py.",
            "lids": lids,
        }, f, indent=2)
        f.write("\n")
    print("wrote", os.path.normpath(a.out))


if __name__ == "__main__":
    main()
