#!/usr/bin/env python3
"""Derive every lid's engraving footprint from the CAD design package.

The lids in ``design.zip`` are ASCII STEP (AP203/214) files, so the geometry can
be read without a CAD kernel: collect the B-rep VERTEX_POINTs of the lid solid
and take their bounding box. Vertices are exact for these parts -- a lid is a
rounded rectangle, and the corner arcs meet the straight edges *at* the extreme
X/Y, so the tangent points are real vertices. (The raw CARTESIAN_POINT set is
NOT usable: it also holds B-spline control points, which overshoot the surface
by up to 8 mm on these files.)

Two shapes of source file appear in the package and both are handled:

  * a standalone ``Lid-<CODE>.STEP`` part -- one solid, read directly;
  * an assembly (``Weekly XS - Assy-SD7-ULS_rev2.step``, the only product with
    no separate lid part) -- walked PRODUCT_DEFINITION -> PRODUCT_DEFINITION_SHAPE
    -> SHAPE_DEFINITION_REPRESENTATION -> ADVANCED_BREP_SHAPE_REPRESENTATION so
    the lid's own solid is measured and the body/plunger are left out.

Every file declares CONVERSION_BASED_UNIT('INCH'), so lengths are scaled by 25.4.

Output is the JSON the SurfAlign lid dialog's Import button reads:
``{"lids": [<name>, ...], "defaults": {name: {width, length, fontSize, rotation,
productCode, ...}}}``. The name is just the product's title -- the engraving
frame is a stored setting, not something encoded into the name. Width is the X
extent (the SHORT side; the case is held long-axis-along-Y, hence the -90 degree
default rotation) and length the Y extent.

    python tools/extract_lid_dimensions.py ../design.zip -o bCNC/lid_dimensions.json

Nano and MagNano are deliberately out of scope and are skipped.
"""

import argparse
import json
import os
import re
import sys
import zipfile

MM_PER_INCH = 25.4

# Design-package folder prefix -> the SKU product code(s) its lid engraves.
# An AM-PM case is two halves sharing ONE lid geometry, so it is configured once
# under the SET code; PillcaseOrder.lid_product_codes() expands that to both
# sides when a scanned SKU decomposes.
PRODUCT_CODES = {
    "SD3ULS":   ("MPC",       "Mission Pill Case"),
    "SD3ULSM":  ("MVC",       "Mission Vitamin Case"),
    "SD7ULS":   ("WXSPC",     "Weekly XS Pill Case"),
    "SD7UL":    ("WPC",       "Weekly Pill Case"),
    "SD7ULM":   ("WVC",       "Weekly Vitamin Case"),
    "SD7XL":    ("WVXC",      "Weekly Vitamin XL Case"),
    "WV2XL":    ("WV2XC",     "Weekly 2XL Case"),
    "SD7-MAGS": ("AMPM-PC",   "Weekly AM-PM Pill Case"),
    "SD7-MAG":  ("AMPM-VC",   "Weekly AM-PM Vitamin Case"),
    "WAPVXL":   ("AMPM-VXL",  "Weekly AM-PM Vitamin XL Case"),
}

# Per-lid engraving setup. The CAD gives geometry only; these are the shop
# values, keyed by product code.
#
#   rotation  -90 everywhere -- the case is held long-axis-along-Y, so the text
#             runs down the lid. The two Mission cases are short enough that the
#             text reads across the lid instead, so they are 0.
#
#   frame     an OVERRIDE of the measured footprint, used only where the true
#             centre is not where the text should sit. get_lid_dimensions()
#             centres the text at (W/2, -L/2), so shortening L walks the centre
#             UP the lid. Both Mission cases need that: at rotation 0 the text
#             wants to sit above the thumb grip, not on it. The measured
#             footprint is still reported and kept in the comment below, because
#             the drawn outline shrinks with the frame -- it is an engraving
#             frame, not the physical lid.
#
#   fontSize is in mm; depth and layerHeight are deliberately left to the global
#   SurfAlign settings, as nothing here determines them.
LID_SETUP = {
    "MPC":      {"fontSize": 20, "rotation": 0,   "frame": (44.0, 50.0)},  # measured 44.45 x 82.55
    "MVC":      {"fontSize": 24, "rotation": 0,   "frame": (53.0, 62.0)},  # measured 53.34 x 95.12
    "WXSPC":    {"fontSize": 18, "rotation": -90, "frame": None},
    "WPC":      {"fontSize": 20, "rotation": -90, "frame": None},
    "WVC":      {"fontSize": 24, "rotation": -90, "frame": None},
    "WVXC":     {"fontSize": 24, "rotation": -90, "frame": None},
    "WV2XC":    {"fontSize": 25, "rotation": -90, "frame": None},
    "AMPM-PC":  {"fontSize": 17, "rotation": -90, "frame": None},
    "AMPM-VC":  {"fontSize": 22, "rotation": -90, "frame": None},
    "AMPM-VXL": {"fontSize": 24, "rotation": -90, "frame": None},
}

# Out of scope per the brief, and any superseded revision kept in the package.
SKIP = re.compile(r"magnano|nano pill|nano vitamin|old design", re.I)

_CARTESIAN = re.compile(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)", re.I)
_VERTEX = re.compile(r"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)", re.I)
_INCH = re.compile(r"CONVERSION_BASED_UNIT\s*\(\s*'inch'", re.I)


def _entities(text):
    """{id: body} for every ``#N = BODY;`` instance, with line wrapping undone."""
    flat = re.sub(r"\n(?!#)", " ", text.replace("\r", ""))
    return {int(m.group(1)): " ".join(m.group(2).split())
            for m in re.finditer(r"#(\d+)\s*=\s*(.*?);", flat, re.S)}


def _vertices(entities, roots=None):
    """Every vertex coordinate, either in the whole file or under ``roots``."""
    if roots is None:
        bodies = list(entities.items())
    else:
        refs = {i: [int(x) for x in re.findall(r"#(\d+)", b)]
                for i, b in entities.items()}
        seen, stack = set(), list(roots)
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(refs.get(n, ()))
        bodies = [(i, entities[i]) for i in seen if i in entities]

    out = []
    for _id, body in bodies:
        m = _VERTEX.match(body)
        if not m:
            continue
        point = _CARTESIAN.match(entities.get(int(m.group(1)), ""))
        if not point:
            continue
        try:
            xyz = [float(v) for v in point.group(1).split(",")][:3]
        except ValueError:
            continue
        if len(xyz) == 3:
            out.append(xyz)
    return out


def _lid_roots(entities):
    """Representation ids of every solid whose PRODUCT_DEFINITION is named Lid-*."""
    refs = {i: [int(x) for x in re.findall(r"#(\d+)", b)]
            for i, b in entities.items()}
    named = {i: re.match(r"PRODUCT_DEFINITION\s*\(\s*'([^']*)'", b, re.I)
             for i, b in entities.items()}
    lid_defs = set(i for i, m in named.items()
                   if m and m.group(1).lower().startswith("lid"))
    shapes = {i: refs[i][-1] for i, b in entities.items()
              if re.match(r"PRODUCT_DEFINITION_SHAPE", b, re.I) and refs.get(i)}

    # A SHAPE_REPRESENTATION may delegate the actual B-rep to a second
    # representation through SHAPE_REPRESENTATION_RELATIONSHIP; follow both ways.
    linked = {}
    for i, b in entities.items():
        if re.match(r"SHAPE_REPRESENTATION_RELATIONSHIP", b, re.I) and len(refs[i]) >= 2:
            a, c = refs[i][0], refs[i][1]
            linked.setdefault(a, []).append(c)
            linked.setdefault(c, []).append(a)

    roots = []
    for i, b in entities.items():
        if not re.match(r"SHAPE_DEFINITION_REPRESENTATION", b, re.I) or len(refs[i]) < 2:
            continue
        if shapes.get(refs[i][0]) in lid_defs:
            roots.append(refs[i][1])
            roots.extend(linked.get(refs[i][1], ()))
    return roots


def measure(text):
    """(short_mm, long_mm, thickness_mm) of the lid solid in one STEP file."""
    entities = _entities(text)
    scale = MM_PER_INCH if _INCH.search(text) else 1.0

    verts = _vertices(entities)
    roots = _lid_roots(entities)
    if roots:
        # An assembly: measure only the lid, not the body it is sitting on.
        scoped = _vertices(entities, roots)
        if scoped:
            verts = scoped
    if not verts:
        return None

    extents = sorted((max(v[i] for v in verts) - min(v[i] for v in verts)) * scale
                     for i in range(3))
    thickness, short, length = extents
    return short, length, thickness


def _sources(root):
    """(product_folder, filename, text) for each candidate STEP file."""
    if zipfile.is_zipfile(root):
        with zipfile.ZipFile(root) as z:
            for name in sorted(z.namelist()):
                if not name.lower().endswith((".step", ".stp")) or SKIP.search(name):
                    continue
                parts = name.split("/")
                if len(parts) < 3:
                    continue
                yield parts[1], parts[-1], z.read(name).decode("utf-8", "replace")
        return
    for dirpath, _dirs, files in os.walk(root):
        for fn in sorted(files):
            path = os.path.join(dirpath, fn)
            if not fn.lower().endswith((".step", ".stp")) or SKIP.search(path):
                continue
            rel = os.path.relpath(path, root).replace("\\", "/").split("/")
            if len(rel) < 2:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                yield rel[-3] if len(rel) >= 3 else rel[0], fn, fh.read()


def _folder_code(folder):
    """'SD7-MAGS - Weekly AMPM Pill' -> 'SD7-MAGS'. Longest prefix wins, so
    SD7-MAGS is not swallowed by SD7-MAG and SD3ULSM not by SD3ULS."""
    name = folder.strip().upper()
    for code in sorted(PRODUCT_CODES, key=len, reverse=True):
        if name.startswith(code):
            return code
    return re.split(r"[^A-Z0-9-]", name)[0]


def collect(root):
    """{design_code: (product_code, title, short, long, thickness, source)}."""
    found = {}
    for folder, filename, text in _sources(root):
        code = _folder_code(folder)
        if code not in PRODUCT_CODES:
            continue
        is_lid_part = filename.lower().startswith("lid")
        if not is_lid_part and not re.search(r"assy", filename, re.I):
            continue
        # The standalone lid part always wins; an assembly is only a fallback.
        if code in found and found[code][5][1] and not is_lid_part:
            continue
        dims = measure(text)
        if not dims:
            continue
        product_code, title = PRODUCT_CODES[code]
        found[code] = (product_code, title) + dims + ((filename, is_lid_part),)
    return found


def _fmt(value):
    """2.dp, but no trailing '.00' -- an overridden frame is a round number and
    should read as one."""
    return ("%.2f" % value).rstrip("0").rstrip(".")


def build(found):
    """The importable config. The lid NAME is just the product's title: the
    engraving frame is stored as `width`/`length` beside the other settings, so
    a lid can be renamed, and its size corrected, independently."""
    lids, defaults, report = [], {}, []
    for code in sorted(found, key=lambda c: PRODUCT_CODES[c][0]):
        product_code, title, short, length, thickness, (src, _part) = found[code]
        setup = LID_SETUP.get(product_code, {})
        frame = setup.get("frame") or (short, length)
        name = title
        lids.append(name)
        defaults[name] = {
            "width": round(frame[0], 2),
            "length": round(frame[1], 2),
            "fontSize": setup.get("fontSize"),
            "depth": None,
            "layerHeight": None,
            "rotation": setup.get("rotation"),
            "productCode": product_code,
        }
        report.append((code, product_code, title, short, length, thickness,
                       setup.get("frame"), setup.get("rotation"),
                       setup.get("fontSize"), src))
    return {"lids": lids, "defaults": defaults}, report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package", help="design.zip, or the unpacked folder")
    ap.add_argument("-o", "--output", help="write the importable lid JSON here")
    args = ap.parse_args(argv)

    found = collect(args.package)
    data, report = build(found)

    missing = set(PRODUCT_CODES) - set(found)
    print("%-10s %-10s %-30s %15s %15s %6s %5s %4s"
          % ("DESIGN", "PRODUCT", "TITLE", "MEASURED (WxL)",
             "ENGRAVE FRAME", "THICK", "ROT", "FONT"))
    for (code, pc, title, short, length, thickness,
         frame, rotation, font, _src) in report:
        measured = "%.2f x %.2f" % (short, length)
        engrave = ("%s x %s *" % (_fmt(frame[0]), _fmt(frame[1]))
                   if frame else measured)
        print("%-10s %-10s %-30s %15s %15s %6.2f %5s %4s"
              % (code, pc, title, measured, engrave, thickness,
                 rotation if rotation is not None else "-",
                 font if font is not None else "-"))
    if any(r[6] for r in report):
        print("\n* engraving frame overrides the measured footprint to shift the "
              "text centre off the thumb grip")
    if missing:
        print("\nNo lid geometry found for: %s" % ", ".join(sorted(missing)),
              file=sys.stderr)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        print("\nWrote %d lids to %s" % (len(data["lids"]), args.output))
    else:
        print()
        json.dump(data, sys.stdout, indent=2)
        print()
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
