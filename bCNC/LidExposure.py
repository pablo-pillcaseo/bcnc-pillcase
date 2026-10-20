"""Exposure calibration for the lid camera: one fixed exposure for every lid.

Automatic exposure meters the whole photo. Over the black fixture it opens up
until the average looks mid-grey, so light lids and the white marker borders
clip, and every photo is exposed differently. Fixed settings fix both: a lid's
brightness then depends only on the lid, which is what telling Matte Black from
Gunmetal, or Navy from Dark Blue, needs.

The calibration takes two lids, one after the other, in the same spot:

  1. The brightest lid (Aluminum). Found on the bed, then photographed at a
     series of exposures to find the longest one at which neither it nor the
     marker borders clip. Gain stays at its lowest (it only adds noise, and
     nothing moves, so a longer exposure costs nothing). The white balance
     preset whose marker borders come out most neutral is chosen too.
  2. The darkest lid (Matte Black), swapped in at the same spot. Photographed
     once at those settings and measured on the same area, shrunk a little for
     placement, so it never has to be found on the black fixture. Its pixels
     must sit clear of the sensor's floor, or its colour and texture are lost.

`take(aec, agc, wb)` is the caller's: it sets the camera and returns a JPEG.
Everything here is measurement, so it runs on saved photos as well.
"""

import time

import numpy as np

try:
    import cv2 as cv
except ImportError:
    cv = None

import BltCamera
import LidVision

# Highest allowed for the lid's and the borders' brightest pixels (of 255):
# headroom for light that drifts a little brighter later.
TARGET_HI = 235
# The lid's brightest pixels, ignoring the top 0.5%: a few specks of glare
# must not darken every photo. The borders are flat paper: their top 1%.
LID_PERCENTILE = 99.5
BORDER_PERCENTILE = 99.0
# The area measured on both lids: the first lid's outline, shrunk by this much
# beyond LidLearn's edge trim, so a second lid set down a few mm off still
# covers all of it.
PLACEMENT_MM = 3.0
TRIM_MM = 2.0
# The dark lid's median (brightest channel), of 255.
DARK_GOOD = 30
DARK_LOW = 15
# A pixel this dark (brightest channel) has lost its colour.
CRUSHED = 10
AEC_MIN_TRY = 4
FIRST_AEC = 120             # for finding the lid: well inside the range
DEFAULT_WB = 1              # sunny; only the starting point, see _choose_wb


class CalibrationError(Exception):
    pass


class Area:
    """Where to measure: the bright lid's area and the marker borders, in the
    rectified frame of the first photo (anchored on its lowest marker, so the
    same for every photo while the markers stay put)."""

    def __init__(self, ref, region, ppm, lid_mask, ring_mask, marker_mm, plane=None):
        self.ref = ref
        # The first photo's marker fit: used when a photo is too dark (short
        # exposures) to find the markers in. The camera does not move between
        # the photos of one calibration.
        self.plane = plane
        self.region = region
        self.ppm = ppm
        self.lid_mask = lid_mask
        self.ring_mask = ring_mask
        self.marker_mm = marker_mm


def _ring(plane, region, ppm, shape):
    """The white borders around the markers, as LidVision's white reference
    takes them."""
    outer = np.zeros(shape[:2], np.uint8)
    inner = np.zeros(shape[:2], np.uint8)
    for poly in LidVision._marker_polys_px(plane, region, ppm, LidVision.QUIET_ZONE * 0.8):
        cv.fillConvexPoly(outer, poly, 255)
    for poly in LidVision._marker_polys_px(plane, region, ppm, LidVision.QUIET_ZONE * 0.2):
        cv.fillConvexPoly(inner, poly, 255)
    return ((outer > 0) & (inner == 0)).astype(np.uint8) * 255


def find_area(jpeg, candidates, marker_mm, background=None):
    """Find the bright lid in `jpeg` -> Area."""
    res = LidVision.identify(jpeg, candidates, lambda key: None, marker_mm, background=background)
    if res.plane is None:
        raise CalibrationError("No markers in view: the camera cannot measure.")
    if res.lid is None:
        raise CalibrationError("No lid found. Put the bright lid where lids go, fully in view.")
    if res.lid.length_is_min:
        raise CalibrationError("The lid runs off the edge of the view. Move it fully into view.")
    k = int(round((TRIM_MM + PLACEMENT_MM) * res.ppm)) * 2 + 1
    lid = cv.erode(res.lid.mask, np.ones((k, k), np.uint8))
    if cv.countNonZero(lid) < 200:
        raise CalibrationError("The lid is too small to measure.")
    ring = _ring(res.plane, res.region, res.ppm, lid.shape)
    return Area(res.plane.ref, res.region, res.ppm, lid, ring, marker_mm, res.plane), res


def measure(jpeg, area):
    """Brightness of the measured lid area and of the marker borders in one
    photo, from the camera's own values (no white balance)."""
    img = LidVision.decode_jpeg(jpeg)
    markers = LidVision.detect_markers(img)
    if area.ref in markers:
        plane = LidVision.fit_plane(markers, area.marker_mm, ref=area.ref)
    elif area.plane is not None:
        plane = area.plane          # too dark to see the markers: nothing moved
    else:
        raise CalibrationError("Marker %d is not in view any more: did the camera or "
                               "the fixture move?" % area.ref)
    view, valid, _M = LidVision.rectify(img, plane, area.region, area.ppm)
    lid = view[(area.lid_mask > 0) & (valid > 0)]
    ring = view[(area.ring_mask > 0) & (valid > 0)]
    if len(lid) < 100:
        raise CalibrationError("The measured area is out of view.")
    lid_max = lid.max(axis=1)
    out = {
        "lid_hi": float(np.percentile(lid_max, LID_PERCENTILE)),
        "lid_median": float(np.median(lid_max)),
        "lid_crushed": float((lid_max <= CRUSHED).mean()),
        "border_hi": None, "border_chroma": None,
    }
    if len(ring) >= 50:
        out["border_hi"] = float(np.percentile(ring.max(axis=1), BORDER_PERCENTILE))
        lab = LidVision._lab(np.median(ring, axis=0).reshape(1, 3))[0]
        out["border_chroma"] = float(np.hypot(lab[1], lab[2]))
    return out


def _fits(m):
    return m["lid_hi"] <= TARGET_HI and (m["border_hi"] is None or m["border_hi"] <= TARGET_HI)


def _search(take, area, wb, lo=None, hi=None, log=print):
    """Longest exposure (gain 0) at which nothing clips, by bisection:
    brightness rises with exposure, so each photo halves the range."""
    def at(aec):
        m = measure(take(aec, 0, wb), area)
        log("  exposure %4d: lid brightest %3.0f, borders %s" % (
            aec, m["lid_hi"], "-" if m["border_hi"] is None else "%3.0f" % m["border_hi"]))
        return m

    lo = AEC_MIN_TRY if lo is None else lo
    hi = BltCamera.AEC_MAX if hi is None else hi
    m_lo = at(lo)
    if not _fits(m_lo):
        if lo == AEC_MIN_TRY:
            raise CalibrationError("Too bright even at the shortest exposure: the light "
                                   "on the lid is too strong, or it reflects straight "
                                   "into the camera.")
        return _search(take, area, wb, None, lo, log)
    m_hi = at(hi)
    if _fits(m_hi):
        return hi, m_hi
    while hi - lo > max(4, lo // 30):
        mid = (lo + hi) // 2
        m = at(mid)
        if _fits(m):
            lo, m_lo = mid, m
        else:
            hi = mid
    return lo, m_lo


def _choose_wb(take, area, aec, log=print):
    """The white balance preset that leaves the marker borders most neutral:
    the camera's colour then needs the least correction, and no channel is
    starved. Measured a little under the chosen exposure, so nothing clips."""
    best = None
    for wb in sorted(k for k in BltCamera.WB_PRESETS if k):
        m = measure(take(max(AEC_MIN_TRY, int(aec * 0.85)), 0, wb), area)
        if m["border_chroma"] is None:
            return DEFAULT_WB
        log("  white balance %-7s: borders off-white by %.1f" % (BltCamera.WB_PRESETS[wb],
                                                              m["border_chroma"]))
        if best is None or m["border_chroma"] < best[1]:
            best = (wb, m["border_chroma"])
    return best[0]


def calibrate_bright(take, candidates, marker_mm, background=None, log=print):
    """Step 1, the bright lid on the bed. Returns (settings, area, measured)."""
    log("Finding the lid...")
    area, _res = find_area(take(FIRST_AEC, 0, DEFAULT_WB), candidates, marker_mm, background)
    log("Finding the longest exposure that does not clip...")
    aec, m = _search(take, area, DEFAULT_WB, log=log)
    log("Choosing the white balance...")
    wb = _choose_wb(take, area, aec, log=log)
    if wb != DEFAULT_WB:
        log("Exposure again, for that white balance...")
        aec, m = _search(take, area, wb, max(AEC_MIN_TRY, aec // 2),
                         min(BltCamera.AEC_MAX, aec * 2), log=log)
    settings = {"aec": int(aec), "agc": 0, "wb": int(wb)}
    return settings, area, m


def check_dark(take, settings, area, bright, log=print):
    """Step 2, the dark lid in the same spot. Returns (measured, verdict, ok)."""
    m = measure(take(settings["aec"], settings["agc"], settings["wb"]), area)
    log("  dark lid: median %.0f, %.1f%% crushed to black" % (m["lid_median"],
                                                            100 * m["lid_crushed"]))
    if m["lid_median"] > 0.6 * bright["lid_median"]:
        return m, ("That still looks like the bright lid (median %.0f, the bright lid "
                   "was %.0f). Swap in the dark lid at the same spot and measure again."
                   % (m["lid_median"], bright["lid_median"])), False
    if m["lid_median"] >= DARK_GOOD and m["lid_crushed"] < 0.02:
        return m, "Good: the dark lid sits well clear of black (median %.0f of 255)." \
            % m["lid_median"], True
    if m["lid_median"] >= DARK_LOW:
        return m, ("Usable, but the dark lid is quite dark (median %.0f of 255): dark lids "
                   "will be harder to tell apart. More, softer light on the lid would help."
                   % m["lid_median"]), True
    return m, ("Too dark: the dark lid is near black (median %.0f of 255) at the exposure "
               "the bright lid allows. The lids span more brightness than the camera can: "
               "use softer, more even light (less glare on the bright lid), then calibrate "
               "again." % m["lid_median"]), False


def record(settings, bright, dark, verdict):
    """What is saved: the settings, plus what they were chosen from."""
    rec = dict(settings)
    rec.update({
        "wb_name": BltCamera.WB_PRESETS[settings["wb"]],
        "calibrated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bright_lid_hi": round(bright["lid_hi"], 1),
        "bright_lid_median": round(bright["lid_median"], 1),
        "border_hi": None if bright["border_hi"] is None else round(bright["border_hi"], 1),
        "dark_lid_median": round(dark["lid_median"], 1),
        "dark_lid_crushed": round(dark["lid_crushed"], 4),
        "verdict": verdict,
    })
    return rec
