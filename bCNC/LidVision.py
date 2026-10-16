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

import math

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
        self.size_mm = size_mm          # (short, long)
        self.centre_mm = centre_mm
        self.angle = angle
        self.mask = mask                # in the rectified view
        self.box_px = box_px            # 4x2 rectified-view corners
        self.method = method            # "background" or "edges"


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
def _blob_to_lid(contours, shape, ppm, method, min_fill=0.7):
    """Largest plausible lid outline among `contours` -> Lid, or None."""
    best = None
    for c in contours:
        area = cv.contourArea(c)
        if area < (MIN_LID_MM * ppm) ** 2:
            continue
        (cx, cy), (w, h), ang = cv.minAreaRect(c)
        short, long_ = sorted((w / ppm, h / ppm))
        if short < MIN_LID_MM or short > MAX_LID_MM[0] or long_ > MAX_LID_MM[1]:
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
    box = cv.boxPoints(((cx, cy), (w, h), ang))
    dims = sorted((w / ppm, h / ppm))
    return Lid((dims[0], dims[1]), (cx / ppm, cy / ppm), ang, lid_mask, box, method)


def refine_lid(view, lid, ppm, method):
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
    refined = _blob_to_lid(contours, view.shape, ppm, method)
    if refined is None:
        return lid
    # GrabCut can bleed into a bed of a similar colour. Keep the rough answer
    # unless the refined one is a small correction to it.
    if any(abs(a - b) > 4.0 for a, b in zip(refined.size_mm, lid.size_mm)):
        return lid
    return refined


def find_lid_by_background(view, valid, background, exclude, ppm):
    """The lid as what changed since the empty-bed photo."""
    a = cv.GaussianBlur(cv.cvtColor(view, cv.COLOR_BGR2LAB), (0, 0), 1.5).astype(np.int16)
    b = cv.GaussianBlur(cv.cvtColor(background, cv.COLOR_BGR2LAB), (0, 0), 1.5).astype(np.int16)
    diff = np.abs(a - b)
    # Lightness counts for less: a shadow changes L, not a and b.
    score = np.maximum(diff[..., 0] // 2, np.maximum(diff[..., 1], diff[..., 2]) * 2)
    mask = (score > 18).astype(np.uint8) * 255
    mask[(valid == 0) | (exclude > 0)] = 0
    k = cv.getStructuringElement(cv.MORPH_ELLIPSE, (int(ppm * 3) | 1, int(ppm * 3) | 1))
    mask = cv.morphologyEx(mask, cv.MORPH_OPEN, k)
    mask = cv.morphologyEx(mask, cv.MORPH_CLOSE, k, iterations=2)
    contours, _h = cv.findContours(mask, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
    lid = _blob_to_lid(contours, mask.shape, ppm, "background")
    return lid and refine_lid(view, lid, ppm, "background")


def find_lid_by_edges(view, valid, exclude, ppm):
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
    edges[cv.erode(valid, np.ones((9, 9), np.uint8)) == 0] = 0
    edges[cv.dilate(exclude, np.ones((5, 5), np.uint8)) > 0] = 0
    k = cv.getStructuringElement(cv.MORPH_ELLIPSE, (int(ppm * 2) | 1, int(ppm * 2) | 1))
    closed = cv.morphologyEx(edges, cv.MORPH_CLOSE, k)
    contours, _h = cv.findContours(closed, cv.RETR_LIST, cv.CHAIN_APPROX_SIMPLE)
    lid = _blob_to_lid(contours, view.shape, ppm, "edges", min_fill=0.85)
    return lid and refine_lid(view, lid, ppm, "edges")


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
def size_error(measured, lid_size):
    """How many sigmas a measured (short, long) is from a lid's size."""
    want = sorted(lid_size)
    err = 0.0
    for m, w in zip(measured, want):
        sigma = SIZE_SIGMA_MM + SIZE_SIGMA_FRAC * w
        err += ((m - w) / sigma) ** 2
    return math.sqrt(err)


def score(candidates, measured_size, colour, reference_colour):
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
            s_err = size_error(measured_size, c["size"])
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


# --------------------------------------------------------------- the lot
def identify(jpeg_or_img, candidates, reference_colour, marker_mm=DEFAULT_MARKER_MM,
             size_scale=1.0, background=None):
    """Everything above, in order. Never raises for a bad scene: what could not
    be done is in `result.warnings`, and confidence falls accordingly.

    `background`: dict from `background_record`, or None.
    """
    res = Result()
    img = decode_jpeg(jpeg_or_img) if isinstance(jpeg_or_img, (bytes, bytearray)) else jpeg_or_img
    annotated = img.copy()

    markers = detect_markers(img)
    for corners in markers.values():
        cv.polylines(annotated, [corners.astype(np.int32)], True, (255, 160, 0), 2)

    use_bg = bool(background) and background.get("ref") in markers \
        and abs(background.get("marker_mm", 0) - marker_mm) < 1e-6
    plane = fit_plane(markers, marker_mm, ref=background["ref"] if use_bg else None)
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
        if use_bg:
            region, ppm = tuple(background["region"]), background["ppm"]
        else:
            region = visible_region(img.shape, plane)
            ppm = fit_ppm(region)
        view, valid, M = rectify(img, plane, region, ppm)
        white = white_reference(view, plane, region, ppm)
        if white is None:
            res.warnings.append("No white marker border found: colour is not corrected.")
        view = white_balance(view, white)
        res.rectified, res.region = view, region
        exclude = marker_mask(view.shape, plane, region, ppm)

        if use_bg:
            lid = find_lid_by_background(view, valid, background["image"], exclude, ppm)
        if lid is None:
            if background and not use_bg:
                res.warnings.append("Empty-bed photo not used (its anchor marker is "
                                    "not in view): finding the lid by its edges.")
            lid = find_lid_by_edges(view, valid, exclude, ppm)

        if lid is None:
            res.warnings.append("No lid found on the bed.")
        else:
            lid.size_mm = (lid.size_mm[0] * size_scale, lid.size_mm[1] * size_scale)
            res.lid = lid
            colour = colour_of_region(view, lid.mask, ppm)
            box_img = _map(np.linalg.inv(M), lid.box_px).astype(np.int32)
            cv.polylines(annotated, [box_img], True, (0, 220, 0), 3)

    res.colour = colour
    res.colour_lab = None if colour is None else colour.median
    if colour is None and lid is None and plane is not None:
        res.matches, res.none_probability = [], 1.0
    else:
        res.matches, res.none_probability = score(
            candidates, lid.size_mm if lid else None, colour, reference_colour)
    res.annotated = annotated
    return res


def background_record(result, marker_mm):
    """What to keep from an empty-bed photo's Result, or None if unusable."""
    if result.plane is None or result.rectified is None:
        return None
    return {"image": result.rectified, "region": list(result.region),
            "ppm": result.rectified.shape[1] / (result.region[2] - result.region[0]),
            "ref": result.plane.ref, "marker_mm": marker_mm}
