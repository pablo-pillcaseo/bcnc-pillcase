"""Which of the tote's lids is on the machine: size and colour from one photo.

The photo comes from the camera on the BLTouch board (BltCamera). Printed ArUco
markers (tools/print_fiducials.py) stuck around the fixture make it measurable:

  1. Markers. Every marker is a square of known side. One marker alone fixes
     the bed plane's scale and perspective. With more, `fit_plane` solves one
     homography that makes ALL of them perfect squares of that size, which
     averages out each one's corner noise. Their positions on the bed are not
     needed, so markers can be stuck anywhere flat and moved freely.
  2. Rectify. The photo is warped to a top-down view in millimetres, anchored
     on the lowest-numbered marker. The frame depends only on where that marker
     is, not on where the head happened to be when the photo was taken.
  3. White balance. The white border printed around each marker is a known
     white, so the photo's colour cast is measured from it and removed.
  4. Find the lid. If an empty-bed photo was saved, the lid is whatever differs
     from it. That works on any fixture colour. Otherwise the lid is the largest
     rectangular outline in the picture.
  5. Measure. The lid's minimum-area rectangle gives its (short, long) size in
     mm. Its colour is taken from the inside of the outline, edges trimmed.
  6. Score. Each candidate (a lid from the scanned tote) is scored on how far
     the measured size is from the lid's configured width/length, and how far
     the colour is from that colour's thumbnail. A "none of these" hypothesis is
     scored too, so a lid that fits nothing gets low confidence, not a
     confident pick of the least-bad candidate.

Two candidates with the same lid and colour (two navy Weekly Vitamin lids with
different text) cannot be told apart by a camera. They are grouped and reported
together; which text goes first stays the operator's call, as before.

Pure functions over numpy arrays, so everything here is tested with synthetic
photos and no hardware.
"""

import json
import math
import os

try:
    import numpy as np
except ImportError:
    np = None

try:
    import cv2 as cv
except ImportError:
    cv = None


# --------------------------------------------------------------- constants
ARUCO_DICT_NAME = "DICT_4X4_50"
DEFAULT_MARKER_MM = 30.0
# Resolution of the rectified view. 4 px/mm resolves a lid to a quarter mm,
# far finer than the 3 mm between the closest two lid sizes.
PX_PER_MM = 4.0
# Largest rectified view, in pixels per side. A camera looking nearly edge-on
# at the bed would otherwise ask for an enormous warp.
MAX_RECTIFIED_PX = 2400
# The rectified view never extends further than this from the anchor marker.
MAX_REGION_MM = 400.0

# The white border printed around each marker, as a fraction of its side.
# Used as the white reference and kept out of lid detection.
QUIET_ZONE = 0.25

# Smallest and largest object taken for a lid (short side, long side). The
# configured lids run from ~33 x 50 mm to ~62 x 250 mm.
MIN_LID_MM = 20.0
MAX_LID_MM = (100.0, 320.0)

# Size tolerance per dimension: a fixed part for fit and segmentation, plus a
# part that grows with size for scale error.
SIZE_SIGMA_MM = 2.5
SIZE_SIGMA_FRAC = 0.03
# Colour tolerance in (L-weighted) Lab units. Thumbnails are product swatches,
# not photos taken under this camera's light, so this is looser than the
# distance between two well-separated colours. Colours learned from confirmed
# lids (ColourMemory) match far more tightly than this.
COLOUR_SIGMA = 10.0
# Lightness is the least trustworthy part of a camera colour (exposure).
L_WEIGHT = 0.5
# "None of these": scored as a candidate that is ~2.5 sigma off on both size and
# colour. A real lid that is further off than that loses to it.
NONE_LOG_LIKELIHOOD = -6.0


def available():
    """OpenCV with ArUco (4.7+) and numpy: what the vision needs."""
    return cv is not None and np is not None and hasattr(cv, "aruco")


# ----------------------------------------------------------------- results
class PlaneFit:
    """Image -> bed-plane homography, mm, y down, anchored on marker `ref`."""

    def __init__(self, H, layout, ref, rms_mm, marker_mm):
        self.H = H                  # 3x3, image px -> plane mm
        self.layout = layout        # {id: 4x2 corners in plane mm}
        self.ref = ref
        self.rms_mm = rms_mm        # corner residual: how well the markers agree
        self.marker_mm = marker_mm


class Lid:
    """What was found on the bed."""

    def __init__(self, size_mm, centre_mm, angle, mask, box_px, method):
        # (short, long), or (width, visible length) when the lid runs off the
        # edge of the view (length_is_min): then the length is only a minimum.
        self.size_mm = size_mm
        self.length_is_min = False
        self.width_is_min = False
        self.centre_mm = centre_mm
        self.angle = angle
        self.mask = mask                # in the rectified view
        self.box_px = box_px            # 4x2 rectified-view corners
        self.method = method            # "background" or "edges"


def describe_size(lid):
    """'48.3 x 221.0 mm', or '48.3 x ≥225.5 mm' when the length runs off the view."""
    w, l = lid.size_mm
    return "%s%.1f x %s%.1f mm" % ("≥" if lid.width_is_min else "", w,
                                   "≥" if lid.length_is_min else "", l)


class Match:
    """One candidate group's score."""

    def __init__(self, key, candidates, probability, size_error, colour_error, notes):
        self.key = key
        self.candidates = candidates    # the tote rows this group stands for
        self.probability = probability
        self.size_error = size_error    # sigmas, None when size was not measured
        self.colour_error = colour_error
        self.notes = notes

    @property
    def first(self):
        return self.candidates[0]


class Result:
    def __init__(self):
        self.matches = []           # best first
        self.none_probability = 1.0
        self.lid = None
        self.plane = None
        self.colour_lab = None
        self.colour = None          # Colour of the lid, for ColourMemory.learn
        self.warnings = []
        self.annotated = None       # BGR photo with what was found drawn on it
        self.rectified = None       # the white-balanced top-down view
        self.region = None          # (x0, y0, x1, y1) mm of `rectified`
        self.ppm = None             # its pixels per mm
        self.valid = None           # which pixels of `rectified` the camera saw
        self.lid_outline = None     # the lid's box in photo pixels (4x2), for labelling

    @property
    def best(self):
        return self.matches[0] if self.matches else None

    @property
    def confidence(self):
        return self.best.probability if self.best else 0.0


# ------------------------------------------------------------------ images
def decode_jpeg(data):
    img = cv.imdecode(np.frombuffer(data, np.uint8), cv.IMREAD_COLOR)
    if img is None:
        raise ValueError("not a JPEG the camera could have sent")
    return img


def _aruco_dictionary():
    return cv.aruco.getPredefinedDictionary(getattr(cv.aruco, ARUCO_DICT_NAME))


def marker_image(marker_id, side_px):
    """A marker as a black-on-white image, no border (the caller adds one)."""
    d = _aruco_dictionary()
    if hasattr(cv.aruco, "generateImageMarker"):
        return cv.aruco.generateImageMarker(d, int(marker_id), int(side_px))
    return cv.aruco.drawMarker(d, int(marker_id), int(side_px))


def detect_markers(img):
    """{id: 4x2 float32 image corners}, clockwise from the marker's top-left."""
    gray = cv.cvtColor(img, cv.COLOR_BGR2GRAY) if img.ndim == 3 else img
    d = _aruco_dictionary()
    if hasattr(cv.aruco, "ArucoDetector"):
        params = cv.aruco.DetectorParameters()
        params.cornerRefinementMethod = cv.aruco.CORNER_REFINE_SUBPIX
        corners, ids, _rej = cv.aruco.ArucoDetector(d, params).detectMarkers(gray)
    else:
        params = cv.aruco.DetectorParameters_create()
        params.cornerRefinementMethod = cv.aruco.CORNER_REFINE_SUBPIX
        corners, ids, _rej = cv.aruco.detectMarkers(gray, d, parameters=params)
    out = {}
    if ids is None:
        return out
    for c, i in zip(corners, ids.flatten()):
        out[int(i)] = c.reshape(4, 2).astype(np.float32)
    return out


# ------------------------------------------------------------ plane fitting
def _square(side):
    h = side / 2.0
    return np.array([[-h, -h], [h, -h], [h, h], [-h, h]], np.float64)


def _rigid_fit(model, observed):
    """`model` moved (rotated + translated, never scaled) onto `observed`."""
    mc = model.mean(axis=0)
    oc = observed.mean(axis=0)
    u, _s, vt = np.linalg.svd((model - mc).T @ (observed - oc))
    r = (u @ vt).T
    if np.linalg.det(r) < 0:        # a reflection is not a marker's pose
        vt[-1] *= -1
        r = (u @ vt).T
    return (model - mc) @ r.T + oc


def _map(H, pts):
    pts = np.asarray(pts, np.float64).reshape(-1, 1, 2)
    return cv.perspectiveTransform(pts, H).reshape(-1, 2)


def fit_plane(markers, marker_mm=DEFAULT_MARKER_MM, ref=None, iterations=12):
    """One homography that makes every visible marker a `marker_mm` square.

    Alternates between fitting the homography to the current layout estimate
    and re-fitting each marker's pose (as a rigid, correctly sized square) to
    where that homography puts it. The anchor marker never moves, which fixes
    the frame. Returns None with no markers.
    """
    if not markers:
        return None
    ids = sorted(markers)
    if ref is None or ref not in markers:
        ref = ids[0]
    square = _square(marker_mm)
    H = cv.getPerspectiveTransform(markers[ref].astype(np.float32), square.astype(np.float32))
    layout = {ref: square}
    for i in ids:
        if i != ref:
            layout[i] = _rigid_fit(square, _map(H, markers[i]))

    if len(ids) > 1:
        src = np.concatenate([markers[i] for i in ids]).astype(np.float64)
        for _ in range(iterations):
            dst = np.concatenate([layout[i] for i in ids])
            H, _m = cv.findHomography(src, dst, 0)
            for i in ids:
                if i != ref:
                    layout[i] = _rigid_fit(square, _map(H, markers[i]))

    src = np.concatenate([markers[i] for i in ids]).astype(np.float64)
    dst = np.concatenate([layout[i] for i in ids])
    rms = float(np.sqrt(np.mean(np.sum((_map(H, src) - dst) ** 2, axis=1))))
    return PlaneFit(H, layout, ref, rms, marker_mm)


def visible_region(img_shape, plane):
    """The plane-mm box the photo covers, clipped around the anchor marker."""
    h, w = img_shape[:2]
    corners = _map(plane.H, [[0, 0], [w, 0], [w, h], [0, h]])
    x0, y0 = corners.min(axis=0)
    x1, y1 = corners.max(axis=0)
    lim = MAX_REGION_MM
    return (max(x0, -lim), max(y0, -lim), min(x1, lim), min(y1, lim))


def region_transform(region, ppm):
    """Plane mm -> rectified-view px."""
    x0, y0, _x1, _y1 = region
    return np.array([[ppm, 0, -x0 * ppm], [0, ppm, -y0 * ppm], [0, 0, 1]], np.float64)


def rectify(img, plane, region, ppm=PX_PER_MM):
    """Top-down view of `region` (mm) at `ppm`, plus the image->view matrix."""
    x0, y0, x1, y1 = region
    size = (max(1, int(round((x1 - x0) * ppm))), max(1, int(round((y1 - y0) * ppm))))
    M = region_transform(region, ppm) @ plane.H
    view = cv.warpPerspective(img, M, size, flags=cv.INTER_LINEAR,
                              borderMode=cv.BORDER_CONSTANT, borderValue=(0, 0, 0))
    valid = cv.warpPerspective(np.full(img.shape[:2], 255, np.uint8), M, size,
                               flags=cv.INTER_NEAREST)
    return view, valid, M


def fit_ppm(region, ppm=PX_PER_MM):
    """`ppm`, lowered if needed so the rectified view stays a sane size."""
    x0, y0, x1, y1 = region
    longest = max(x1 - x0, y1 - y0, 1e-6)
    return min(ppm, MAX_RECTIFIED_PX / longest)


def _marker_polys_px(plane, region, ppm, grow):
    """Each marker's outline in the rectified view, grown by `grow` x its side."""
    T = region_transform(region, ppm)
    out = []
    for corners in plane.layout.values():
        c = corners.mean(axis=0)
        big = c + (corners - c) * (1.0 + 2.0 * grow)
        out.append(_map(T, big).astype(np.int32))
    return out


def marker_mask(shape, plane, region, ppm, grow=QUIET_ZONE + 0.1):
    """Mask of the markers and their white borders: never part of a lid."""
    mask = np.zeros(shape[:2], np.uint8)
    for poly in _marker_polys_px(plane, region, ppm, grow):
        cv.fillConvexPoly(mask, poly, 255)
    return mask


def white_reference(view, plane, region, ppm):
    """Median BGR of the white borders around the markers, or None."""
    outer = np.zeros(view.shape[:2], np.uint8)
    inner = np.zeros(view.shape[:2], np.uint8)
    for poly in _marker_polys_px(plane, region, ppm, QUIET_ZONE * 0.8):
        cv.fillConvexPoly(outer, poly, 255)
    for poly in _marker_polys_px(plane, region, ppm, QUIET_ZONE * 0.2):
        cv.fillConvexPoly(inner, poly, 255)
    ring = (outer > 0) & (inner == 0)
    px = view[ring]
    if len(px) < 50:
        return None
    ref = np.median(px.reshape(-1, 3), axis=0)
    # Too dark to be white paper: the border is missing or in shadow.
    return ref if ref.min() > 60 else None


def white_balance(view, white):
    """Scale channels so `white` becomes a neutral light grey."""
    if white is None:
        return view
    gains = 245.0 / np.maximum(white.astype(np.float64), 1.0)
    return np.clip(view.astype(np.float64) * gains, 0, 255).astype(np.uint8)


# --------------------------------------------------------------- the lid
def _blob_to_lid(contours, shape, ppm, method, min_fill=0.7, max_width=MAX_LID_MM[0],
                 min_width=MIN_LID_MM):
    """Largest plausible lid outline among `contours` -> Lid, or None.

    `max_width` / `min_width`: nothing wider or narrower is a lid. Kept just
    outside the widest and narrowest lids that could be on the bed, so a fixture
    plate (a bigger rectangle than the lid) or a strip of paper or tape (a
    smaller one) is not taken for it.
    """
    best = None
    for c in contours:
        area = cv.contourArea(c)
        if area < (min_width * ppm) ** 2:
            continue
        (cx, cy), (w, h), ang = cv.minAreaRect(c)
        short, long_ = sorted((w / ppm, h / ppm))
        if short < min_width or short > max_width or long_ > MAX_LID_MM[1]:
            continue
        # A lid is a rectangle; a blob that fills little of its box is clutter.
        if area / max(w * h, 1.0) < min_fill:
            continue
        if best is None or area > best[0]:
            best = (area, c, (cx, cy), (w, h), ang)
    if best is None:
        return None
    _area, c, (cx, cy), (w, h), ang = best
    lid_mask = np.zeros(shape[:2], np.uint8)
    cv.drawContours(lid_mask, [c], -1, 255, cv.FILLED)
    box = _robust_box(lid_mask, cv.boxPoints(((cx, cy), (w, h), ang)), ppm)
    # The lid's pixels are those inside its box: a strip outside it is not lid.
    inside = np.zeros(shape[:2], np.uint8)
    cv.fillConvexPoly(inside, box.astype(np.int32), 255)
    lid_mask &= inside
    sides = (float(np.linalg.norm(box[1] - box[0])) / ppm, float(np.linalg.norm(box[3] - box[0])) / ppm)
    dims = sorted(sides)
    centre = box.mean(axis=0)
    return Lid((dims[0], dims[1]), (float(centre[0]) / ppm, float(centre[1]) / ppm), ang,
               lid_mask, box, method)


def _robust_box(mask, box, ppm, bin_mm=2.0):
    """The lid's rectangle from its blob, ignoring strips along its sides.

    The smallest rectangle around a blob has to take in every bump on it. A
    lid's shadow, or its side wall, shows on a light patch of fixture beside it
    (white paper, a label) and not on the black around it, so the blob grows
    strips along part of a side, and that rectangle comes out wider and tilted.
    Here each side goes where most of the blob's edge is: the blob is cut into
    slices across that side, and the side is the median of the slices' ends.
    A strip along less than half of a side, or a rounded corner, moves nothing.
    The long sides also set the angle, the same way, before the ends are found.
    """
    ys, xs = np.nonzero(mask)
    if len(xs) < 100:
        return box.astype(np.float32)
    pts = np.stack([xs, ys], axis=1).astype(np.float64)
    p0, p1, _p2, p3 = box.astype(np.float64)
    e1 = p1 - p0 if np.linalg.norm(p1 - p0) >= np.linalg.norm(p3 - p0) else p3 - p0
    e1 /= np.linalg.norm(e1)                    # along the long side
    c = pts.mean(axis=0)
    step = max(2.0, bin_mm * ppm)

    def slices(a, b):
        """Centre of each slice along `a`, and the min / max of `b` in it."""
        k = np.floor((a - a.min()) / step).astype(int)
        n = int(k.max()) + 1
        lo, hi = np.full(n, np.inf), np.full(n, -np.inf)
        np.minimum.at(lo, k, b)
        np.maximum.at(hi, k, b)
        full = np.bincount(k, minlength=n) >= step      # a slice with real width
        centres = a.min() + (np.arange(n) + 0.5) * step
        return centres[full], lo[full], hi[full]

    for straighten in (True, False):
        e2 = np.array([-e1[1], e1[0]])
        u, v = (pts - c) @ e1, (pts - c) @ e2
        uc, vlo, vhi = slices(u, v)
        if len(uc) < 3:
            return box.astype(np.float32)
        if straighten:
            slopes = []
            for edge in (vlo, vhi):
                near = np.abs(edge - np.median(edge)) < 2 * ppm
                if near.sum() >= 5:
                    slopes.append(np.polyfit(uc[near], edge[near], 1)[0])
            if not slopes:
                break
            t = np.arctan(np.median(slopes))
            e1 = np.cos(t) * e1 + np.sin(t) * e2
            continue
    v0, v1 = np.median(vlo), np.median(vhi)
    _vc, ulo, uhi = slices(v, u)
    if len(ulo) < 3:
        return box.astype(np.float32)
    u0, u1 = np.median(ulo), np.median(uhi)
    corners = [c + u0 * e1 + v0 * e2, c + u1 * e1 + v0 * e2,
               c + u1 * e1 + v1 * e2, c + u0 * e1 + v1 * e2]
    return np.array(corners, np.float32)


def refine_lid(view, lid, ppm, method, max_width=MAX_LID_MM[0], min_width=MIN_LID_MM):
    """Tighten a lid's outline with GrabCut, seeded from the rough box.

    The rough outline is a few pixels off either way: an edge band has width,
    and a background difference is blurred. GrabCut decides each boundary
    pixel by colour models of the lid and of what surrounds it, which also
    copes with lids that have dots of a second colour.
    """
    h, w = view.shape[:2]
    margin = int(6 * ppm)
    x, y, bw, bh = cv.boundingRect(lid.box_px.astype(np.int32))
    x0, y0 = max(0, x - margin), max(0, y - margin)
    x1, y1 = min(w, x + bw + margin), min(h, y + bh + margin)
    roi = view[y0:y1, x0:x1]
    box = (lid.box_px - [x0, y0]).astype(np.int32)

    def poly_mask(grow_mm):
        c = box.mean(axis=0)
        d = box - c
        n = np.linalg.norm(d, axis=1, keepdims=True)
        poly = (c + d + d / np.maximum(n, 1e-6) * grow_mm * ppm * 1.41).astype(np.int32)
        m = np.zeros(roi.shape[:2], np.uint8)
        cv.fillConvexPoly(m, poly, 1)
        return m

    gc = np.full(roi.shape[:2], cv.GC_BGD, np.uint8)
    gc[poly_mask(3) > 0] = cv.GC_PR_BGD
    gc[poly_mask(0) > 0] = cv.GC_PR_FGD
    gc[poly_mask(-3) > 0] = cv.GC_FGD
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv.grabCut(roi, gc, None, bgd, fgd, 4, cv.GC_INIT_WITH_MASK)
    except cv.error:
        return lid
    fg = np.isin(gc, (cv.GC_FGD, cv.GC_PR_FGD)).astype(np.uint8) * 255
    k = cv.getStructuringElement(cv.MORPH_ELLIPSE, (int(ppm * 2) | 1, int(ppm * 2) | 1))
    fg = cv.morphologyEx(fg, cv.MORPH_OPEN, k)
    full = np.zeros((h, w), np.uint8)
    full[y0:y1, x0:x1] = fg
    contours, _h = cv.findContours(full, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_NONE)
    refined = _blob_to_lid(contours, view.shape, ppm, method, max_width=max_width,
                           min_width=min_width)
    if refined is None:
        return lid
    # GrabCut can bleed into a bed of a similar colour. Keep the rough answer
    # unless the refined one is a small correction to it.
    if any(abs(a - b) > 4.0 for a, b in zip(refined.size_mm, lid.size_mm)):
        return lid
    return refined


# How far the empty bed may be out of line with a photo and still match it.
SHIFT_TOLERANCE_MM = 1.5

# Where the empty bed is darker than this (8-bit L), a lightness change of more
# than DARK_BED_DIFF counts as a lid: see find_lid_by_background.
DARK_BED_L = 80
DARK_BED_DIFF = 12


def find_lid_by_background(view, valid, background, exclude, ppm, max_width=MAX_LID_MM[0],
                           min_width=MIN_LID_MM):
    """The lid as what changed since the empty-bed photo."""
    if background.shape[:2] != view.shape[:2]:
        # An older record whose scale was rounded: off by a pixel at most.
        background = cv.resize(background, (view.shape[1], view.shape[0]))
    lab_bed = cv.GaussianBlur(cv.cvtColor(background, cv.COLOR_BGR2LAB), (0, 0), 1.5)
    a = cv.GaussianBlur(cv.cvtColor(view, cv.COLOR_BGR2LAB), (0, 0), 1.5).astype(np.int16)
    b = lab_bed.astype(np.int16)
    # Compared with what the empty bed has anywhere within SHIFT_TOLERANCE_MM,
    # not only at the same spot: the markers line two photos up to about half
    # a mm, and raised things (the fence, bolt heads) shift a little when the
    # camera has moved. A lid changes a whole area, far more than that.
    k = np.ones((int(2 * SHIFT_TOLERANCE_MM * ppm) | 1,) * 2, np.uint8)
    lo = cv.erode(lab_bed, k).astype(np.int16)
    hi = cv.dilate(lab_bed, k).astype(np.int16)
    diff = np.maximum(np.maximum(a - hi, lo - a), 0)
    # Lightness counts for less: a shadow changes L, not a and b.
    score = np.maximum(diff[..., 0] // 2, np.maximum(diff[..., 1], diff[..., 2]) * 2)
    changed = score > 18
    # Except where the empty bed is dark (the black fixture): a shadow hardly
    # changes black, while a dark lid on it is only a little lighter (15-22 in
    # L on a Matte Black lid) - under the threshold above, but over its whole
    # area. Without this, such a lid shows only where it covers something light
    # and falls apart into patches. A hole's edge shifted by a fraction of a mm
    # passes too, but only as a thin line, which the opening below removes.
    # (Measured: the lid comes out the same anywhere from 10 to 14.)
    changed |= (b[..., 0] < DARK_BED_L) & (diff[..., 0] > DARK_BED_DIFF)
    mask = changed.astype(np.uint8) * 255
    mask[(valid == 0) | (exclude > 0)] = 0
    k = cv.getStructuringElement(cv.MORPH_ELLIPSE, (int(ppm * 3) | 1, int(ppm * 3) | 1))
    mask = cv.morphologyEx(mask, cv.MORPH_OPEN, k)
    mask = cv.morphologyEx(mask, cv.MORPH_CLOSE, k, iterations=2)
    contours, _h = cv.findContours(mask, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
    lid = _blob_to_lid(contours, mask.shape, ppm, "background", max_width=max_width,
                       min_width=min_width)
    return lid and refine_lid(view, lid, ppm, "background", max_width, min_width)


def find_lid_by_edges(view, valid, exclude, ppm, max_width=MAX_LID_MM[0], min_width=MIN_LID_MM):
    """The lid as the largest closed rectangular outline (no empty-bed photo).

    Every closed outline is a candidate, nested ones included: the fixture or
    the edge of the bed is an outline too, and it encloses the lid.
    """
    # Edges in lightness AND in each colour axis: a navy lid on a brown bed is
    # nearly the same grey, and only the colour channels see where it ends.
    lab = cv.GaussianBlur(cv.cvtColor(view, cv.COLOR_BGR2LAB), (0, 0), 2.0)
    edges = np.zeros(view.shape[:2], np.uint8)
    for ch in cv.split(lab):
        edges |= cv.Canny(ch, 20, 60)
    # The edge of the photo is not an edge of anything: drop what is found
    # there, then draw the edge of the view in as a line, so a lid that runs
    # off the photo still has a closed outline (its own three sides plus the
    # edge of the view).
    inner = cv.erode(valid, np.ones((9, 9), np.uint8))
    edges[inner == 0] = 0
    edges |= inner - cv.erode(inner, np.ones((3, 3), np.uint8))
    edges[cv.dilate(exclude, np.ones((5, 5), np.uint8)) > 0] = 0
    k = cv.getStructuringElement(cv.MORPH_ELLIPSE, (int(ppm * 2) | 1, int(ppm * 2) | 1))
    closed = cv.morphologyEx(edges, cv.MORPH_CLOSE, k)
    contours, _h = cv.findContours(closed, cv.RETR_LIST, cv.CHAIN_APPROX_SIMPLE)
    lid = _blob_to_lid(contours, view.shape, ppm, "edges", min_fill=0.85, max_width=max_width,
                       min_width=min_width)
    return lid and refine_lid(view, lid, ppm, "edges", max_width, min_width)


# ------------------------------------------------------------ fixture corner
# A lid "in the corner": its side along the fence within this angle, and its
# fence-side edge within this distance of the fence line.
CORNER_ANGLE_DEG = 4.0
CORNER_EDGE_MM = 6.0


class LidCorner:
    """Where lids sit: pushed into the same corner of the fixture, so every
    lid's upper-left corner is in one place and its sides run the same way.

    Kept in the bed plane, in mm from marker `ref` (the markers are stuck on
    the fixture), so it holds wherever the camera is. Recorded from one lid
    that outlines well (`corner_from_result`).

      corner      [x, y] mm: the lid's corner at the fixture's corner
      along       unit vector along the lid's long side, away from the corner
      across      unit vector along its short side, away from the fence
    """

    def __init__(self, ref, corner, along, across):
        self.ref = int(ref)
        self.corner = np.asarray(corner, np.float64)
        self.along = np.asarray(along, np.float64)
        self.across = np.asarray(across, np.float64)

    def to_dict(self):
        return {"ref": self.ref, "corner": self.corner.tolist(),
                "along": self.along.tolist(), "across": self.across.tolist()}

    @classmethod
    def from_dict(cls, d):
        return cls(d["ref"], d["corner"], d["along"], d["across"])


def corner_from_result(res):
    """The LidCorner of the lid found in `res`, taking its upper-left corner as
    seen by the camera. None without a lid or markers."""
    if res.lid is None or res.plane is None or res.lid_outline is None:
        return None
    box = res.lid.box_px.astype(np.float64)                 # rectified px
    img = np.asarray(res.lid_outline, np.float64)           # the same corners, photo px
    i = int(np.argmin(img[:, 0] + img[:, 1]))               # upper left in the photo
    prev, nxt = box[(i - 1) % 4], box[(i + 1) % 4]
    c = box[i]
    a, b = nxt - c, prev - c
    along, across = (a, b) if np.linalg.norm(a) >= np.linalg.norm(b) else (b, a)
    x0, y0 = res.region[0], res.region[1]
    corner_mm = c / res.ppm + [x0, y0]
    return LidCorner(res.plane.ref, corner_mm, along / np.linalg.norm(along),
                     across / np.linalg.norm(across))


def snap_to_corner(lid, corner, region, ppm, valid):
    """The lid anchored on the fixture's corner, if it is in it: its fence
    sides become the corner's lines, and only its far side and far end come
    from what was found. Those are the edges the camera sees best; the end at
    the fence (a dark lid on the black fixture, under the fence's shadow) is
    the one it loses. Returns (lid, in_corner)."""
    c = (corner.corner - [region[0], region[1]]) * ppm      # rectified px
    box = lid.box_px.astype(np.float64)
    u = (box - c) @ corner.along / ppm                      # mm along the long side
    v = (box - c) @ corner.across / ppm                     # mm across, from the fence
    sides = [box[(k + 1) % 4] - box[k] for k in range(4)]
    long_side = max(sides, key=np.linalg.norm)
    cos = abs(float(long_side @ corner.along)) / max(np.linalg.norm(long_side), 1e-9)
    angle = np.degrees(np.arccos(min(1.0, cos)))
    if angle > CORNER_ANGLE_DEG or abs(v.min()) > CORNER_EDGE_MM or u.min() < -CORNER_EDGE_MM:
        return lid, False
    length, width = float(u.max()), float(v.max())
    if length <= 0 or width <= 0:
        return lid, False
    L, W = corner.along * length * ppm, corner.across * width * ppm
    new_box = np.array([c, c + L, c + L + W, c + W], np.float32)
    mask = np.zeros(lid.mask.shape, np.uint8)
    cv.fillConvexPoly(mask, new_box.astype(np.int32), 255)
    mask[valid == 0] = 0
    centre = new_box.mean(axis=0)
    dims = sorted((length, width))
    out = Lid((dims[0], dims[1]), (float(centre[0]) / ppm, float(centre[1]) / ppm), lid.angle,
              mask, new_box, lid.method)
    return out, True


# --------------------------------------------- edges, from the fixture corner
# With the corner known, a lid in it has only two edges left to find: its far
# side, a straight line parallel to the fence, and its far end, across it. A
# straight edge present along most of the lid stands out in the photo itself,
# whatever the camera angle does to the glossy fixture's reflections - which
# is where comparing with the empty bed fails for a dark lid on the black
# fixture after the camera has moved.
EDGE_PPM = 4.0
EDGE_FRAME_MM = (-10.0, 80.0, -10.0, 270.0)   # across from, to; along from, to
EDGE_BAND_MM = (8.0, 70.0)                     # along: inside every lid's length
EDGE_MIN_RATIO = 15.0                          # edge strength over its background
EDGE_SNAP_MM = 6.0                             # edge vs map width: same edge
# One lid between the fence and the edge: its contrast with what lies beyond
# must beat its own variation by this much, and be at least UNIFORM_MIN. With
# no lid the "contrast" is the smaller of the two by far (measured: 1-2 against
# 7-10); a Matte Black lid on the black fixture is 2x and more.
UNIFORM_RATIO = 1.3
UNIFORM_MIN = 4.0


def _corner_frame(img, plane, corner):
    """The photo resampled so x runs across the lid (from the fence) and y
    along it (from the corner), EDGE_PPM px per mm -> (BGR, seen mask, H),
    H mapping frame px to photo px."""
    v0, v1, u0, u1 = EDGE_FRAME_MM
    A = np.array([[corner.across[0], corner.along[0], corner.corner[0]],
                  [corner.across[1], corner.along[1], corner.corner[1]], [0, 0, 1.0]])
    S = np.array([[1 / EDGE_PPM, 0, v0], [0, 1 / EDGE_PPM, u0], [0, 0, 1.0]])
    H = np.linalg.inv(plane.H) @ A @ S
    size = (int((v1 - v0) * EDGE_PPM), int((u1 - u0) * EDGE_PPM))
    inv = np.linalg.inv(H)
    frame = cv.warpPerspective(img, inv, size, flags=cv.INTER_LINEAR)
    seen = cv.warpPerspective(np.full(img.shape[:2], 255, np.uint8), inv, size,
                              flags=cv.INTER_NEAREST)
    return frame, seen, H


def _edge_strength(lab, seen, across):
    """Edge strength at each pixel, across the lid (`across`) or along it."""
    g = np.zeros(lab.shape[:2], np.float32)
    for ch, wgt in ((0, 1.0), (1, 2.0), (2, 2.0)):
        c = cv.GaussianBlur(lab[..., ch].astype(np.float32), (0, 0), 1.2)
        d = cv.Sobel(c, cv.CV_32F, 1 if across else 0, 0 if across else 1, ksize=3)
        g = np.maximum(g, np.abs(d) * wgt)
    g[seen == 0] = np.nan
    return g


def _px(mm, origin):
    return int(round((mm - origin) * EDGE_PPM))


def _band(lab, seen, u0, u1, v0, v1):
    """Mean Lab of a band of the frame, or None if it was not seen."""
    vf, _v1f, uf, _u1f = EDGE_FRAME_MM
    r0, r1, c0, c1 = _px(u0, uf), _px(u1, uf), _px(v0, vf), _px(v1, vf)
    if r1 <= r0 or c1 <= c0 or r0 < 0 or c0 < 0:
        return None
    m = seen[r0:r1, c0:c1] > 0
    if m.sum() < 20:
        return None
    return lab[r0:r1, c0:c1][m].astype(np.float64).mean(axis=0)


def _de(a, b):
    """Lab distance between two 8-bit Lab means, lightness at full weight: a
    dark lid differs from the black fixture in little else."""
    d = a - b
    d[0] *= 100 / 255
    return float(np.linalg.norm(d))


def corner_edges(img, plane, corner, min_width, max_width):
    """The lid's far side as seen from the corner: its width (mm), how clearly
    it stands out, and whether what lies between it and the fence looks like
    one lid. None without enough of the corner in view."""
    import warnings
    frame, seen, _H = _corner_frame(img, plane, corner)
    lab = cv.cvtColor(frame, cv.COLOR_BGR2LAB)
    vf, _v1f, uf, _u1f = EDGE_FRAME_MM
    g = _edge_strength(lab, seen, across=True)
    rows = slice(_px(EDGE_BAND_MM[0], uf), _px(EDGE_BAND_MM[1], uf))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)       # all-unseen columns
        prof = np.nan_to_num(np.nanmedian(g[rows, :], axis=0), nan=0.0)
    lo, hi = _px(max(min_width - 4, 10), vf), _px(max_width + 4, vf)
    if hi <= lo + 4 or not (seen[rows, lo:hi] > 0).any():
        return None
    i = lo + int(np.argmax(prof[lo:hi]))
    width = vf + i / EDGE_PPM
    base = float(np.median(prof[lo:hi])) + 1.0
    strength = float(prof[i]) / base
    # One lid between the fence and the edge: even, and unlike what is beyond.
    inner = [_band(lab, seen, u, u + 6, 6, width - 6) for u in (12, 30, 48, 66)]
    outer = _band(lab, seen, EDGE_BAND_MM[0], EDGE_BAND_MM[1], width + 4, width + 16)
    uniform = False
    contrast = spread = None
    if all(x is not None for x in inner) and outer is not None:
        mean = np.mean(inner, axis=0)
        spread = max(_de(x, mean) for x in inner)
        contrast = _de(mean, outer)
        uniform = contrast >= max(UNIFORM_MIN, UNIFORM_RATIO * spread)
    return {"width": width, "strength": strength, "uniform": uniform,
            "contrast": contrast, "spread": spread}


def _corner_box(corner, region, ppm, width, length):
    """The rectified-view box of a lid in the corner, `width` x `length` mm."""
    c = (corner.corner - [region[0], region[1]]) * ppm
    L, W = corner.along * length * ppm, corner.across * width * ppm
    return np.array([c, c + L, c + L + W, c + W], np.float32)


def _box_lid(box, shape, valid, ppm, angle, method):
    mask = np.zeros(shape[:2], np.uint8)
    cv.fillConvexPoly(mask, box.astype(np.int32), 255)
    mask[valid == 0] = 0
    sides = (float(np.linalg.norm(box[1] - box[0])) / ppm, float(np.linalg.norm(box[3] - box[0])) / ppm)
    centre = box.mean(axis=0)
    dims = sorted(sides)
    return Lid((dims[0], dims[1]), (float(centre[0]) / ppm, float(centre[1]) / ppm), angle,
               mask, box, method)


def lid_from_corner_edges(edges, corner, region, ppm, valid, shape):
    """A lid found by its far side alone (nothing else found it): from the
    corner to that side, and along to where the view ends - its length is
    then only a minimum. None unless the edge and the lid are both clear."""
    if edges is None or edges["strength"] < EDGE_MIN_RATIO or not edges["uniform"]:
        return None
    # Along the lid until it leaves the view (or the map's known ground).
    c = (corner.corner - [region[0], region[1]]) * ppm
    mid = c + corner.across * edges["width"] / 2 * ppm
    h, w = valid.shape
    length = 0.0
    for step in np.arange(1.0, MAX_LID_MM[1] + 1.0, 1.0):
        x, y = (mid + corner.along * step * ppm).astype(int)
        if not (0 <= x < w and 0 <= y < h) or valid[y, x] == 0:
            break
        length = step
    if length < MIN_LID_MM:
        return None
    box = _corner_box(corner, region, ppm, edges["width"], length)
    return _box_lid(box, shape, valid, ppm, 0.0, "corner edges")


def mark_clipped(lid, valid, ppm):
    """Flag a lid that runs off the edge of the view, and which way.

    Its pixels next to the edge of the view tell which sides of its box are
    cut off. A cut across the box's long axis means only part of the length is
    in view: the width is still measured exactly, the length becomes a minimum.
    """
    # The view's own border is an edge too: with an empty-bed photo the view
    # is that photo's area, and a lid can run past it.
    outside = (valid == 0).astype(np.uint8)
    outside[[0, -1], :] = 1
    outside[:, [0, -1]] = 1
    near_edge = cv.dilate(outside, np.ones((int(3 * ppm) | 1,) * 2, np.uint8))
    ys, xs = np.nonzero((lid.mask > 0) & (near_edge > 0))
    if len(xs) == 0:
        return lid
    p0, p1, _p2, p3 = lid.box_px.astype(np.float64)
    pts = np.stack([xs, ys], axis=1).astype(np.float64) - p0
    axes = (p1 - p0, p3 - p0)
    ts, tols = [], []
    for axis in axes:
        length2 = max(float(axis @ axis), 1e-6)
        ts.append(pts @ axis / length2)             # 0..1 along this side
        tols.append(3 * ppm / math.sqrt(length2))
    clipped = []
    for i in (0, 1):
        t, other = ts[i], ts[1 - i]
        at_end = (t < tols[i]) | (t > 1 - tols[i])
        # Cut off only if the contact runs along a good part of that end: a
        # corner grazing a slanted edge of the view touches both sides.
        spread = float(np.ptp(other[at_end])) if np.any(at_end) else 0.0
        clipped.append(spread > 0.3)
    sides = (np.linalg.norm(p1 - p0) / ppm, np.linalg.norm(p3 - p0) / ppm)
    if clipped[0] and clipped[1]:
        lid.size_mm = tuple(sorted(sides))
        lid.length_is_min = lid.width_is_min = True
    elif clipped[0] or clipped[1]:
        cut = 0 if clipped[0] else 1
        lid.size_mm = (sides[1 - cut], sides[cut])  # (width, visible length)
        lid.length_is_min = True
    return lid


# ----------------------------------------------------------------- colour
def _lab(bgr_pixels):
    """8-bit BGR pixels (N,3) -> real CIE Lab (N,3) floats."""
    lab = cv.cvtColor(bgr_pixels.reshape(-1, 1, 3).astype(np.uint8), cv.COLOR_BGR2LAB)
    lab = lab.reshape(-1, 3).astype(np.float64)
    lab[:, 0] *= 100.0 / 255.0
    lab[:, 1:] -= 128.0
    return lab


def _smooth(h, sigma=0.8):
    """Gaussian blur along every axis of an n-d histogram, so a colour that
    the camera shifted by part of a bin still overlaps its reference."""
    r = 2
    x = np.arange(-r, r + 1)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    k /= k.sum()
    for axis in range(h.ndim):
        # "full" then cropped: "same" returns the longer of the two inputs,
        # which for the 4 lightness bins is the 5-tap kernel.
        h = np.apply_along_axis(lambda v: np.convolve(v, k)[r:r + len(v)], axis, h)
    return h


class Colour:
    """A surface's colour: its median Lab, plus a coarse, smoothed Lab
    histogram. The median alone cannot tell a plain lid from one with dots of
    another colour on the same base (Bahama Blue vs Cherry Blossom); the
    histogram can."""

    BINS = (4, 10, 10)                      # L, a, b
    RANGE = [(0, 100), (-100, 100), (-100, 100)]

    def __init__(self, lab):
        self.median = np.median(lab, axis=0)
        h, _edges = np.histogramdd(lab, bins=self.BINS, range=self.RANGE)
        h = _smooth(h)
        self.hist = h / max(h.sum(), 1e-9)


def colour_of_region(view, mask, ppm):
    """Colour inside `mask`, edges trimmed (they mix lid with bed)."""
    k = int(max(3, ppm * 3)) | 1
    inner = cv.erode(mask, np.ones((k, k), np.uint8))
    px = view[inner > 0]
    if len(px) < 30:
        px = view[mask > 0]
    if len(px) < 30:
        return None
    return Colour(_lab(px))


def colour_of_thumbnail(path):
    """Colour of a thumbnail file: its opaque pixels, centre-weighted."""
    img = cv.imread(path, cv.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 2:
        img = cv.cvtColor(img, cv.COLOR_GRAY2BGR)
    h, w = img.shape[:2]
    y0, y1, x0, x1 = h // 6, h - h // 6, w // 6, w - w // 6
    crop = img[y0:y1, x0:x1]
    if crop.shape[2] == 4:
        px = crop[crop[..., 3] > 128][:, :3]
    else:
        px = crop.reshape(-1, 3)
    if len(px) < 30:
        return None
    return Colour(_lab(px))


def colour_distance(a, b):
    """L-weighted Lab distance between medians, blended with how much the
    a/b histograms fail to overlap (scaled to similar units)."""
    d = a.median - b.median
    d[0] *= L_WEIGHT
    median_d = float(np.sqrt(np.sum(d * d)))
    overlap = float(np.sum(np.sqrt(a.hist * b.hist)))       # Bhattacharyya coeff.
    hist_d = math.sqrt(max(0.0, 1.0 - overlap)) * 40.0
    return 0.65 * median_d + 0.35 * hist_d


class ColourMemory:
    """Colours as THIS camera sees them, learned from lids the operator
    confirmed. A learned colour replaces the thumbnail for matching: it was
    taken under the machine's own light, by its own sensor.

    Kept as a running average of up to `MAX_SAMPLES` confirmations, so one
    odd photo does not define a colour, and a change of lighting is learned
    within a few lids.
    """

    MAX_SAMPLES = 20

    def __init__(self, path=None):
        self.path = path
        self.colours = {}           # key -> {"n": int, "median": [3], "hist": [...]}
        if path:
            try:
                import json
                with open(path) as f:
                    self.colours = json.load(f)
            except (OSError, ValueError):
                self.colours = {}

    def get(self, key):
        rec = self.colours.get(key)
        if not rec:
            return None
        c = Colour.__new__(Colour)
        c.median = np.array(rec["median"], np.float64)
        c.hist = np.array(rec["hist"], np.float64).reshape(Colour.BINS)
        return c

    def learn(self, key, colour):
        if not key or colour is None:
            return
        rec = self.colours.get(key)
        if rec is None:
            rec = {"n": 0, "median": colour.median.tolist(),
                   "hist": colour.hist.ravel().tolist()}
        n = min(rec["n"], self.MAX_SAMPLES - 1)
        w = 1.0 / (n + 1)
        rec["median"] = ((1 - w) * np.array(rec["median"]) + w * colour.median).tolist()
        rec["hist"] = ((1 - w) * np.array(rec["hist"]) + w * colour.hist.ravel()).tolist()
        rec["n"] = n + 1
        self.colours[key] = rec
        self.save()

    def forget(self):
        self.colours = {}
        self.save()

    def save(self):
        if not self.path:
            return
        import json
        try:
            with open(self.path, "w") as f:
                json.dump(self.colours, f)
        except OSError as e:
            print("[lidcam] could not save learned colours:", e)


# ------------------------------------------------------------------ scoring
def size_error(measured, lid_size, length_is_min=False, width_is_min=False):
    """How many sigmas a measured (short, long) is from a lid's size.

    A dimension that is only a minimum (the lid runs off the view) counts
    against a lid only when the lid is shorter than what is already in view.
    """
    want = sorted(lid_size)
    err = 0.0
    for m, w, is_min in zip(measured, want, (width_is_min, length_is_min)):
        sigma = SIZE_SIGMA_MM + SIZE_SIGMA_FRAC * w
        d = max(0.0, m - w) if is_min else m - w
        err += (d / sigma) ** 2
    return math.sqrt(err)


def score(candidates, measured_size, colour, reference_colour, length_is_min=False,
          width_is_min=False):
    """Rank candidates. Returns ([Match], none_probability).

    `candidates`: dicts with `key` (grouping: same lid + colour), `size`
    ((w, l) mm or None) and `colour_key`. `reference_colour(colour_key)` returns
    a Colour or None.
    """
    groups = {}
    for c in candidates:
        groups.setdefault(c["key"], []).append(c)

    scored = []
    for key, members in groups.items():
        c = members[0]
        notes = []
        log_l = 0.0
        s_err = None
        if measured_size is not None and c.get("size"):
            s_err = size_error(measured_size, c["size"], length_is_min, width_is_min)
            log_l -= 0.5 * s_err ** 2
        elif measured_size is not None:
            notes.append("lid size not configured")
            log_l -= 0.5
        c_err = None
        ref = reference_colour(c.get("colour_key")) if colour is not None else None
        if ref is not None:
            c_err = colour_distance(colour, ref) / COLOUR_SIGMA
            log_l -= 0.5 * c_err ** 2
        elif colour is not None:
            notes.append("no thumbnail to compare colour with")
            log_l -= 0.5
        scored.append([key, members, log_l, s_err, c_err, notes])

    logs = [s[2] for s in scored] + [NONE_LOG_LIKELIHOOD]
    top = max(logs)
    weights = [math.exp(v - top) for v in logs]
    total = sum(weights)
    matches = [Match(s[0], s[1], w / total, s[3], s[4], s[5])
               for s, w in zip(scored, weights[:-1])]
    matches.sort(key=lambda m: -m.probability)
    return matches, weights[-1] / total


# ------------------------------------------------------------- empty-bed map
# The bare fixture, as a map in the bed plane rather than as one photo.
#
# An empty-bed photo used to be compared with lids only from the camera
# position it was taken at: the camera is on the probe, its mount is never
# perfectly rigid, and the head is wherever it is. The map is in mm from marker
# `ref`, like everything measured, so any later photo lines up with it from
# wherever the camera now is. It grows to cover what each view adds, and it
# keeps itself current: the bare fixture around every confirmed lid is blended
# in (`learn`), so a camera that crept, light that drifted or a label that
# moved is taken in within a few lids.
MAP_FULL = 8                # this many looks make a map pixel fully known
MAP_MAX_MM = 800.0          # the most the map grows to, either way
LEARN_MARGIN_MM = 15.0      # kept clear around a confirmed lid when learning


class BedMap:
    """The empty bed in the bed plane: `image` (BGR, white-balanced like the
    views it is compared with) over the area from (x0, y0) mm at `ppm`, and
    `count`, how many looks each pixel has had (0: never seen)."""

    def __init__(self, ref, marker_mm, x0, y0, ppm, image, count):
        self.ref = int(ref)
        self.marker_mm = float(marker_mm)
        self.x0, self.y0, self.ppm = float(x0), float(y0), float(ppm)
        self.image = image.astype(np.float32)
        self.count = count.astype(np.uint8)

    # -------------------------------------------------------------- making
    def copy(self):
        """A copy to change while this one is still in use elsewhere."""
        return BedMap(self.ref, self.marker_mm, self.x0, self.y0, self.ppm,
                      self.image.copy(), self.count.copy())

    @classmethod
    def from_result(cls, res, marker_mm):
        """A map from one empty-bed photo's Result, or None if unusable."""
        if res.plane is None or res.rectified is None:
            return None
        valid = res.valid if res.valid is not None else \
            np.full(res.rectified.shape[:2], 255, np.uint8)
        m = cls(res.plane.ref, marker_mm, res.region[0], res.region[1], res.ppm,
                np.zeros(res.rectified.shape, np.float32), np.zeros(valid.shape, np.uint8))
        m.add(res.rectified, valid, res.region, res.ppm, replace=True)
        return m

    @classmethod
    def from_record(cls, rec):
        """A map from an old single-photo record (background_record)."""
        img = rec["image"]
        valid = img.max(axis=2) > 0
        m = cls(rec["ref"], rec.get("marker_mm", DEFAULT_MARKER_MM), rec["region"][0],
                rec["region"][1], rec["ppm"], img, np.zeros(valid.shape, np.uint8))
        m.count[valid] = MAP_FULL
        return m

    def _to_view(self, region, ppm):
        """Affine from map pixels to the pixels of a view of `region` at `ppm`."""
        s = ppm / self.ppm
        return np.float32([[s, 0, (self.x0 - region[0]) * ppm],
                           [0, s, (self.y0 - region[1]) * ppm]])

    def _grow(self, region):
        """Enlarge the map to take in `region` (mm), up to MAP_MAX_MM."""
        h, w = self.count.shape
        x1, y1 = self.x0 + w / self.ppm, self.y0 + h / self.ppm
        nx0, ny0 = min(self.x0, region[0]), min(self.y0, region[1])
        nx1, ny1 = max(x1, region[2]), max(y1, region[3])
        if (nx1 - nx0) > MAP_MAX_MM or (ny1 - ny0) > MAP_MAX_MM:
            return                       # a wild marker fit: do not grow
        ox = int(np.ceil((self.x0 - nx0) * self.ppm))
        oy = int(np.ceil((self.y0 - ny0) * self.ppm))
        nw = max(w + ox, int(np.ceil((nx1 - self.x0) * self.ppm)) + ox)
        nh = max(h + oy, int(np.ceil((ny1 - self.y0) * self.ppm)) + oy)
        if (ox, oy, nw, nh) == (0, 0, w, h):
            return
        img = np.zeros((nh, nw, 3), np.float32)
        cnt = np.zeros((nh, nw), np.uint8)
        img[oy:oy + h, ox:ox + w] = self.image
        cnt[oy:oy + h, ox:ox + w] = self.count
        self.image, self.count = img, cnt
        self.x0, self.y0 = self.x0 - ox / self.ppm, self.y0 - oy / self.ppm

    def add(self, view, valid, region, ppm, keep=None, replace=False):
        """Take in a top-down `view` of `region` (its `valid` pixels, and of
        those only `keep` if given). `replace`: an empty-bed photo, trusted
        outright; otherwise blended in as one more look. Returns the number of
        map pixels taken in."""
        self._grow(region)
        h, w = self.count.shape
        to_map = cv.invertAffineTransform(self._to_view(region, ppm))
        v = cv.warpAffine(view.astype(np.float32), to_map, (w, h), flags=cv.INTER_LINEAR)
        ok = valid if keep is None else cv.bitwise_and(valid, keep)
        ok = cv.warpAffine(ok, to_map, (w, h), flags=cv.INTER_NEAREST) > 0
        if replace:
            self.image[ok] = v[ok]
            self.count[ok] = MAP_FULL
            return int(ok.sum())
        n = self.count[ok].astype(np.float32)
        wgt = (1.0 / (np.minimum(n, MAP_FULL - 1) + 1.0))[:, None]
        self.image[ok] = self.image[ok] * (1 - wgt) + v[ok] * wgt
        self.count[ok] = np.minimum(self.count[ok].astype(np.int32) + 1, MAP_FULL).astype(np.uint8)
        return int(ok.sum())

    def learn(self, res, margin_mm=LEARN_MARGIN_MM):
        """After a lid is confirmed: the bare fixture around it, as seen now,
        becomes one more look. Never the lid itself, nor a margin round it
        (its shadow, its light on the paper beside it). False if the photo
        cannot be used."""
        if (res.lid is None or res.plane is None or res.rectified is None or res.valid is None
                or res.plane.ref != self.ref or res.plane.rms_mm > 1.0):
            return False
        keep = np.full(res.valid.shape, 255, np.uint8)
        # The box grown by the margin on every side, along its own sides: grown
        # along its diagonals, a long thin lid's long sides would hardly move.
        box = res.lid.box_px.astype(np.float64)
        c = box.mean(axis=0)
        e1, e2 = box[1] - box[0], box[3] - box[0]
        half1, half2 = np.linalg.norm(e1) / 2, np.linalg.norm(e2) / 2
        e1, e2 = e1 / max(2 * half1, 1e-6), e2 / max(2 * half2, 1e-6)
        m = margin_mm * res.ppm
        grown = [c + s1 * (half1 + m) * e1 + s2 * (half2 + m) * e2
                 for s1, s2 in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        cv.fillConvexPoly(keep, np.array(grown).astype(np.int32), 0)
        return self.add(res.rectified, res.valid, res.region, res.ppm, keep) > 0

    # ----------------------------------------------------------------- use
    def view(self, region, ppm, shape):
        """The map as a view of `region` at `ppm` -> (BGR image, known mask)."""
        M = self._to_view(region, ppm)
        size = (shape[1], shape[0])
        img = cv.warpAffine(self.image, M, size, flags=cv.INTER_LINEAR)
        known = cv.warpAffine(self.count, M, size, flags=cv.INTER_NEAREST)
        return np.clip(img, 0, 255).astype(np.uint8), ((known > 0) * 255).astype(np.uint8)

    # ------------------------------------------------------------- storage
    def save(self, base):
        """<base>.png (the image), <base>-count.png, <base>.json."""
        cv.imwrite(base + ".png", np.clip(self.image, 0, 255).astype(np.uint8))
        cv.imwrite(base + "-count.png", self.count)
        tmp = base + ".json.tmp"
        with open(tmp, "w") as f:
            json.dump({"ref": self.ref, "marker_mm": self.marker_mm, "x0": self.x0,
                       "y0": self.y0, "ppm": self.ppm}, f)
        os.replace(tmp, base + ".json")

    @classmethod
    def load(cls, base):
        try:
            with open(base + ".json") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return None
        img = cv.imread(base + ".png", cv.IMREAD_COLOR)
        cnt = cv.imread(base + "-count.png", cv.IMREAD_GRAYSCALE)
        if img is None or cnt is None or img.shape[:2] != cnt.shape:
            return None
        return cls(meta["ref"], meta["marker_mm"], meta["x0"], meta["y0"], meta["ppm"], img, cnt)


def load_bed(map_base, record_base=None):
    """The empty-bed map at `map_base`; else one made from an old single-photo
    record at `record_base` (<base>.png / .json); else None."""
    m = BedMap.load(map_base)
    if m is not None or not record_base:
        return m
    try:
        with open(record_base + ".json") as f:
            rec = json.load(f)
        rec["image"] = cv.imread(record_base + ".png", cv.IMREAD_COLOR)
        if rec["image"] is None:
            return None
        return BedMap.from_record(rec)
    except (OSError, ValueError, KeyError):
        return None


# --------------------------------------------------------------- the lot
def identify(jpeg_or_img, candidates, reference_colour, marker_mm=DEFAULT_MARKER_MM,
             size_scale=1.0, background=None, corner=None):
    """Everything above, in order. Never raises for a bad scene: what could not
    be done is in `result.warnings`, and confidence falls accordingly.

    `background`: BedMap (or an old background_record dict), or None.
    `corner`: LidCorner, or None: a lid in the fixture's corner is anchored on it.
    """
    res = Result()
    img = decode_jpeg(jpeg_or_img) if isinstance(jpeg_or_img, (bytes, bytearray)) else jpeg_or_img
    annotated = img.copy()

    markers = detect_markers(img)
    for corners in markers.values():
        cv.polylines(annotated, [corners.astype(np.int32)], True, (255, 160, 0), 2)

    if isinstance(background, dict):
        background = BedMap.from_record(background)
    use_bg = background is not None and background.ref in markers \
        and abs(background.marker_mm - marker_mm) < 1e-6
    plane = fit_plane(markers, marker_mm, ref=background.ref if use_bg else None)
    res.plane = plane

    lid = None
    colour = None
    if plane is None:
        res.warnings.append("No markers in view: size cannot be measured. "
                            "Check the fiducial labels are visible to the camera.")
        # Colour alone, from the middle of the photo.
        h, w = img.shape[:2]
        centre = img[h // 3:2 * h // 3, w // 3:2 * w // 3]
        colour = Colour(_lab(centre.reshape(-1, 3)))
    else:
        if plane.rms_mm > 1.0:
            res.warnings.append("Markers disagree by %.1f mm: is one lifted, curled or "
                                "printed at a different size?" % plane.rms_mm)
        region = visible_region(img.shape, plane)
        ppm = fit_ppm(region)
        view, valid, M = rectify(img, plane, region, ppm)
        white = white_reference(view, plane, region, ppm)
        if white is None:
            res.warnings.append("No white marker border found: colour is not corrected.")
        view = white_balance(view, white)
        res.rectified, res.region, res.ppm, res.valid = view, region, ppm, valid
        exclude = marker_mask(view.shape, plane, region, ppm)
        # The widest and narrowest lids that could be on the bed, plus room for
        # measurement error and size correction. Without candidates, the
        # generic limits.
        widths = [min(c["size"]) for c in candidates if c.get("size")]
        scale = max(size_scale, 0.5)
        max_width = (max(widths) / scale) * 1.1 + 4.0 if widths else MAX_LID_MM[0]
        min_width = max(MIN_LID_MM, (min(widths) / scale) * 0.9 - 4.0) if widths else MIN_LID_MM

        if use_bg:
            bed, known = background.view(region, ppm, view.shape)
            seen = cv.countNonZero(cv.bitwise_and(valid, known)) / max(cv.countNonZero(valid), 1)
            if seen < 0.9:
                res.warnings.append("%d%% of this view is not in the empty-bed map yet: it "
                                    "fills in as lids are confirmed, or save the empty bed "
                                    "(with no lid) from here." % round(100 * (1 - seen)))
            # Ground the map has never seen counts as out of view: a lid
            # running onto it is measured as running off the view.
            lid = find_lid_by_background(view, cv.bitwise_and(valid, known), bed, exclude, ppm,
                                         max_width, min_width)
            if lid is not None:
                valid = cv.bitwise_and(valid, known)
        if lid is None:
            if background and not use_bg:
                res.warnings.append("Empty-bed photo not used (its anchor marker is "
                                    "not in view): finding the lid by its edges.")
            lid = find_lid_by_edges(view, valid, exclude, ppm, max_width, min_width)

        if lid is None and corner is not None and corner.ref == plane.ref:
            # Nothing stood out from the empty bed (a dark lid on the black
            # fixture, seen from a new angle): a clear far side, with one even
            # lid between it and the fence, still finds it.
            lid = lid_from_corner_edges(corner_edges(img, plane, corner, min_width, max_width),
                                        corner, region, ppm, valid, view.shape)
            if lid is not None:
                res.warnings.append("Found by its edges only: check the outline.")
        if lid is None:
            res.warnings.append("No lid found on the bed.")
        else:
            if corner is not None:
                if corner.ref != plane.ref:
                    res.warnings.append("The fixture corner was set with marker %d, which is "
                                        "not the one in use: lid measured on its own."
                                        % corner.ref)
                else:
                    lid, in_corner = snap_to_corner(lid, corner, region, ppm, valid)
                    if not in_corner:
                        res.warnings.append("The lid is not in the fixture's corner: measured "
                                            "on its own.")
                    else:
                        # The far side from its edge, where it is clear and
                        # agrees: sharper than the outline, and a shadow on
                        # the paper beside the lid does not move it.
                        e = corner_edges(img, plane, corner, min_width, max_width)
                        if e is not None and e["strength"] >= EDGE_MIN_RATIO \
                                and abs(e["width"] - lid.size_mm[0]) <= EDGE_SNAP_MM:
                            length = float(np.linalg.norm(lid.box_px[1] - lid.box_px[0])) / ppm
                            lid = _box_lid(_corner_box(corner, region, ppm, e["width"], length),
                                           view.shape, valid, ppm, lid.angle, lid.method)
            lid = mark_clipped(lid, valid, ppm)
            lid.size_mm = (lid.size_mm[0] * size_scale, lid.size_mm[1] * size_scale)
            res.lid = lid
            if lid.length_is_min:
                res.warnings.append(
                    "The lid runs off the edge of the camera's view: %s"
                    % ("only a part of it is measured." if lid.width_is_min else
                       "its width is measured, its length only as at least %.0f mm."
                       % lid.size_mm[1]))
            colour = colour_of_region(view, lid.mask, ppm)
            box_img = _map(np.linalg.inv(M), lid.box_px).astype(np.int32)
            res.lid_outline = box_img
            # Green when the whole lid is in view, yellow when it runs off it.
            cv.polylines(annotated, [box_img], True,
                         (0, 200, 255) if lid.length_is_min else (0, 220, 0), 3)

    res.colour = colour
    res.colour_lab = None if colour is None else colour.median
    if colour is None and lid is None and plane is not None:
        res.matches, res.none_probability = [], 1.0
    else:
        res.matches, res.none_probability = score(
            candidates, lid.size_mm if lid else None, colour, reference_colour,
            bool(lid and lid.length_is_min), bool(lid and lid.width_is_min))
    res.annotated = annotated
    return res


def background_record(result, marker_mm):
    """What to keep from an empty-bed photo's Result, or None if unusable."""
    if result.plane is None or result.rectified is None:
        return None
    return {"image": result.rectified, "region": list(result.region), "ppm": result.ppm,
            "ref": result.plane.ref, "marker_mm": marker_mm}
