"""Learn lid colours and finishes from lids the operator has named.

LidVision compares a lid with a product thumbnail, with hand-picked tolerances.
This keeps every lid the operator names instead - its photo, the lid cut out
and turned long side horizontal, and its measured size - and names the next lid
after the named lids most like it. Nothing is trained: a named lid helps from
the very next photo.

Several ways of describing a lid run side by side, so a test shows which of
them tells this camera's lids apart, and how many named lids each needs:

  colour    LidVision's Colour (median Lab plus a coarse histogram), compared
            with named lids instead of thumbnails.
  palette   the lid's main colours and how much of it each covers, plus pattern
            measures that do not depend on where things are on the lid: how
            much second colour, in how many flecks of what size; edge strength
            and direction (stripes have one); colour change along the lid
            (gradients).
  network   a pretrained image network (MobileNetV2, ImageNet) run on squares of
            a fixed size in mm, its features averaged over the lid. It describes
            texture - flecks, streaks, blotches - whatever their layout, but is
            built to half-ignore colour.
  all       network and palette together: the network for the pattern, the
            palette for the colours.

and two that learn from the named lids themselves (step 2), switched on once
LEARN_FROM lids are named and refitted every REFIT_EVERY lids after that:

  weighted  colour, palette, pattern and network distances added up with
            weights fitted to the named lids, instead of equally: each counts
            as much as it has helped tell this camera's colours apart.
  trained   a small classifier (multinomial logistic regression) on the frozen
            network's features plus the colour and pattern measures, its
            regularisation chosen by cross-validation on the named lids.

Both are refitted only at fixed counts of named lids, and only from the lids
named before, so a guess never benefits from its own answer - and the live
window and the report make the same guesses.

and two for size:

  outline   nearest physical outline in lid_outlines.json (LidVision's way).
            Lids with the same outline are reported together: no photo from
            above can split them.
  learned   nearest named lid by measured size, which takes in this camera's
            own measuring error.

No method has a tolerance picked by hand. Each scales its distances by those
between the lids already named, so they mean the same at lid 5 and lid 500.

One folder holds everything (default ~/lidcam/examples):

  examples.jsonl      one line per named lid, oldest first
  photos/<id>.jpg     the photo as taken
  crops/<id>.png      the lid, long side horizontal, CROP_PPM pixels per mm
  masks/<id>.png      which crop pixels are lid (edges trimmed)
  features/<id>.npz   cached features, rebuilt from the crop when stale

Each line of examples.jsonl is kept for reuse (training a model later, or
re-running any of this on the photos), so every field says what it is:

  id                  also the name of the lid's files
  time                when it was named (Unix seconds)
  size                the lid type as the operator confirmed it, e.g.
                      'Weekly Pill Case'
  colour              its colour code as the operator confirmed it, e.g.
                      'NAVY' (SkuParser.COLOUR_NAMES)
  measured            [short, long] mm as measured, or null if no lid was found
  width_is_min, length_is_min   the lid ran off the view: only a minimum
  method              how the lid was found: 'background' or 'edges'
  crop_ppm, trim_mm   how the crop and mask were made
  marker_mm, empty_bed          the setup: marker size, and whether an
                      empty-bed photo was used to find the lid
  guesses             every method's guess before the answer was given:
                      {method: [answer, confidence] or null}, `ran` (the
                      methods that could run) and `learned_from` (step 2)
"""

import hashlib
import json
import math
import os
import threading
import time
import urllib.request

import numpy as np

try:
    import cv2 as cv
except ImportError:         # LidVision says so; nothing here can run without it
    cv = None

import LidVision

CROP_PPM = 4.0
# Lid edges mix lid with bed, and a moulded lid curves down at its rim.
TRIM_MM = 2.0
PALETTE_K = 4
# A pixel this far (L-weighted Lab) from the lid's main colour is a fleck,
# streak or patch of another colour.
FLECK_DE = 15.0
# Along-the-lid segments, for gradients.
SEGMENTS = 8

# The network sees squares this size, in mm, so a fleck is the same size to it
# on every lid. Fits inside the narrowest lid (33.5 mm) with the edges trimmed.
TILE_MM = 28.0
NET_PX = 224
MAX_TILES = 16
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406])
IMAGENET_STD = np.array([0.229, 0.224, 0.225])

MODEL_URL = ("https://github.com/onnx/models/raw/main/validated/vision/classification/"
             "mobilenet/model/mobilenetv2-12.onnx")
MODEL_SHA256 = "c0c3f76d93fa3fd6580652a45618618a220fced18babf65774ed169de0432ad5"
MODEL_FILE = "mobilenetv2-12.onnx"

# Bump when what a feature means changes: cached features are then rebuilt.
FEATURE_VERSION = 1

BASE_METHODS = ("colour", "palette", "network", "all")
LEARNED_METHODS = ("weighted", "trained")
COLOUR_METHODS = BASE_METHODS + LEARNED_METHODS
SIZE_METHODS = ("outline", "learned")

# Pairs sampled to find the typical distance between two named lids.
SCALE_PAIRS = 400


# ------------------------------------------------------------------ the crop
def lid_crop(view, lid, ppm, out_ppm=CROP_PPM, trim_mm=TRIM_MM):
    """The lid cut out of the rectified view, long side horizontal, at
    `out_ppm`, and a mask of its own pixels with the edges trimmed off.

    Turning by 180 degrees is not undone: nothing measured here depends on it.
    """
    p = lid.box_px.astype(np.float32)
    if np.linalg.norm(p[1] - p[0]) < np.linalg.norm(p[2] - p[1]):
        p = np.roll(p, -1, axis=0)
    k = out_ppm / ppm
    w = max(1, int(round(np.linalg.norm(p[1] - p[0]) * k)))
    h = max(1, int(round(np.linalg.norm(p[2] - p[1]) * k)))
    dst = np.float32([[0, 0], [w, 0], [w, h]])
    A = cv.getAffineTransform(p[:3], dst)
    crop = cv.warpAffine(view, A, (w, h), flags=cv.INTER_AREA if k < 1 else cv.INTER_LINEAR,
                         borderMode=cv.BORDER_CONSTANT)
    mask = cv.warpAffine(lid.mask, A, (w, h), flags=cv.INTER_NEAREST)
    # Pixels from outside the camera's view are pure black after rectifying.
    mask[crop.max(axis=2) == 0] = 0
    t = int(round(trim_mm * out_ppm)) * 2 + 1
    mask = cv.erode(mask, np.ones((t, t), np.uint8))
    return crop, mask


def _lab_image(bgr):
    """8-bit BGR image -> real CIE Lab floats, same scale as LidVision._lab."""
    lab = cv.cvtColor(bgr, cv.COLOR_BGR2LAB).astype(np.float64)
    lab[..., 0] *= 100.0 / 255.0
    lab[..., 1:] -= 128.0
    return lab


# ------------------------------------------------------------------ features
def _palette(px):
    """Main colours of Lab pixels (N,3): (k,4) rows of weight, L, a, b."""
    rng = np.random.default_rng(0)
    if len(px) > 8000:
        px = px[rng.choice(len(px), 8000, replace=False)]
    data = px.astype(np.float32).copy()
    data[:, 0] *= LidVision.L_WEIGHT
    k = min(PALETTE_K, len(data))
    cv.setRNGSeed(0)
    _c, labels, centres = cv.kmeans(data, k, None,
                                    (cv.TERM_CRITERIA_EPS + cv.TERM_CRITERIA_MAX_ITER, 30, 0.5),
                                    3, cv.KMEANS_PP_CENTERS)
    weights = np.bincount(labels.ravel(), minlength=k) / float(len(labels))
    centres = centres.astype(np.float64)
    centres[:, 0] /= LidVision.L_WEIGHT
    keep = weights > 0
    rows = np.column_stack([weights[keep], centres[keep]])
    return rows[np.argsort(-rows[:, 0])]


def _texture(lab, mask, palette, ppm=CROP_PPM):
    """Pattern measures that do not depend on where things are on the lid.

    Every value is flip- and layout-invariant: a splatter lid with its flecks
    elsewhere, or the same lid turned around, measures the same.
    """
    m = mask > 0
    n = max(int(m.sum()), 1)
    L = lab[..., 0].astype(np.float32)
    out = []

    def grad(img, sigma):
        b = cv.GaussianBlur(img, (0, 0), sigma)
        return cv.Sobel(b, cv.CV_32F, 1, 0, ksize=3), cv.Sobel(b, cv.CV_32F, 0, 1, ksize=3)

    # Edge strength, fine (about 0.25 mm) and coarse (about 1 mm), lightness and colour.
    for sigma in (1.0, 4.0):
        gx, gy = grad(L, sigma)
        out.append(math.log1p(float(np.hypot(gx, gy)[m].mean())))
    ca = 0.0
    for ch in (1, 2):
        gx, gy = grad(lab[..., ch].astype(np.float32), 1.0)
        ca += float(np.hypot(gx, gy)[m].mean())
    out.append(math.log1p(ca))

    # Edge direction, 6 bins over 180 degrees, weighted by strength. Measured
    # against the lid's long side, so stripes give one strong bin.
    gx, gy = grad(L, 1.5)
    mag = np.hypot(gx, gy)[m]
    ang = np.mod(np.arctan2(gy, gx)[m], np.pi)
    hist, _ = np.histogram(ang, bins=6, range=(0, np.pi), weights=mag)
    out.extend((hist / max(hist.sum(), 1e-9)).tolist())

    px = lab[m]
    out.extend(np.std(px, axis=0).tolist() if len(px) else [0.0, 0.0, 0.0])

    # A second colour: how much, in how many flecks, how big.
    main = palette[0, 1:]
    d = lab - main
    d[..., 0] *= LidVision.L_WEIGHT
    other = (np.sqrt((d * d).sum(axis=2)) > FLECK_DE) & m
    out.append(float(other.sum()) / n)
    count, _lbl, stats, _c = cv.connectedComponentsWithStats(other.astype(np.uint8), connectivity=8)
    areas = stats[1:, cv.CC_STAT_AREA]
    areas = areas[areas >= 2] / (ppm * ppm)                 # mm^2
    lid_mm2 = n / (ppm * ppm)
    out.append(math.log1p(len(areas) / max(lid_mm2, 1.0) * 1000.0))   # per 1000 mm^2
    out.append(float(np.mean(np.log(areas))) if len(areas) else 0.0)

    # Colour change along the lid: a gradient differs end to end, a plain or
    # patterned lid does not.
    cols = np.array_split(np.arange(lab.shape[1]), SEGMENTS)
    segs = []
    for c in cols:
        sm = m[:, c]
        if sm.sum() > 20:
            segs.append(np.median(lab[:, c][sm], axis=0))
    if len(segs) >= 2:
        segs = np.array(segs)
        diffs = segs[:, None, :] - segs[None, :, :]
        out.append(math.log1p(float(np.sqrt((diffs ** 2).sum(axis=2)).max())))
        out.append(math.log1p(float(np.linalg.norm(segs[0] - segs[-1]))))
    else:
        out.extend([0.0, 0.0])
    return np.array(out, np.float64)


class Network:
    """MobileNetV2 through OpenCV's own ONNX reader: no extra packages."""

    def __init__(self, path):
        self.path = path
        self.net = cv.dnn.readNetFromONNX(path)
        names = self.net.getLayerNames()
        pools = [n for n in names if self.net.getLayer(n).type == "Pooling"]
        if not pools:
            raise ValueError("no pooling layer in %s" % path)
        self.layer = pools[-1]
        with open(path, "rb") as f:
            self.sha = hashlib.sha256(f.read()).hexdigest()[:16]

    def embed(self, crop, mask, ppm=CROP_PPM):
        """Unit-length feature vector of the lid, averaged over its squares."""
        h, w = mask.shape
        t = int(round(TILE_MM * ppm))
        t = max(16, min(t, h, w))
        lid_px = crop[mask > 0]
        fill = np.median(lid_px, axis=0) if len(lid_px) else np.array([128, 128, 128])
        y0 = (h - t) // 2
        tiles = []
        for x0 in range(0, w - t + 1, max(1, t // 2)):
            frac = float((mask[y0:y0 + t, x0:x0 + t] > 0).mean())
            tiles.append((frac, x0))
        good = [x for f, x in tiles if f >= 0.9]
        if not good:                        # a short or clipped lid: its fullest squares
            good = [x for f, x in sorted(tiles, reverse=True)[:3] if f > 0.3]
        if not good:
            return None
        if len(good) > MAX_TILES:
            good = [good[i] for i in np.linspace(0, len(good) - 1, MAX_TILES).round().astype(int)]
        batch = []
        for x0 in good:
            tile = crop[y0:y0 + t, x0:x0 + t].copy()
            # Bed showing through the trimmed edge would read as pattern.
            tile[mask[y0:y0 + t, x0:x0 + t] == 0] = fill
            tile = cv.resize(tile, (NET_PX, NET_PX), interpolation=cv.INTER_LINEAR)
            rgb = tile[..., ::-1].astype(np.float32) / 255.0
            batch.append(((rgb - IMAGENET_MEAN) / IMAGENET_STD).transpose(2, 0, 1))
        # One square at a time: this model's graph has its batch size fixed at 1.
        f = []
        for x in batch:
            self.net.setInput(np.ascontiguousarray(x[None], np.float32))
            f.append(self.net.forward(self.layer).reshape(-1))
        f = np.array(f, np.float64)
        f /= np.maximum(np.linalg.norm(f, axis=1, keepdims=True), 1e-9)
        v = f.mean(axis=0)
        return v / max(np.linalg.norm(v), 1e-9)


def features(crop, mask, network=None):
    """Every description of the lid, as a dict of arrays."""
    lab = _lab_image(crop)
    px = lab[mask > 0]
    if len(px) < 30:
        return None
    colour = LidVision.Colour(px)
    pal = _palette(px)
    out = {
        "version": np.array(FEATURE_VERSION),
        "colour_median": colour.median,
        "colour_hist": colour.hist.ravel(),
        "palette": pal,
        "texture": _texture(lab, mask, pal),
    }
    if network is not None:
        e = network.embed(crop, mask)
        if e is not None:
            out["embedding"] = e
            out["model"] = np.array(network.sha)
    return out


def fetch_model(folder):
    """Download the network once, checked against its known hash. Returns its path."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, MODEL_FILE)
    if os.path.exists(path):
        return path
    tmp = path + ".part"
    with urllib.request.urlopen(MODEL_URL, timeout=60) as r, open(tmp, "wb") as f:
        f.write(r.read())
    with open(tmp, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    if sha != MODEL_SHA256:
        os.remove(tmp)
        raise ValueError("downloaded model does not match its known hash")
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------- storage
class Example:
    """One named lid."""

    def __init__(self, rec):
        self.rec = rec
        self.id = rec["id"]
        self.size = rec["size"]                 # lid name, e.g. 'Weekly Pill Case'
        self.colour = rec["colour"]             # colour code, e.g. 'NAVY'
        self.measured = tuple(rec["measured"]) if rec.get("measured") else None
        self.width_is_min = rec.get("width_is_min", False)
        self.length_is_min = rec.get("length_is_min", False)
        self.features = None


class Store:
    def __init__(self, folder):
        self.folder = folder
        for sub in ("photos", "crops", "masks", "features"):
            os.makedirs(os.path.join(folder, sub), exist_ok=True)
        self.index = os.path.join(folder, "examples.jsonl")
        self.examples = []
        if os.path.exists(self.index):
            with open(self.index) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.examples.append(Example(json.loads(line)))

    def _p(self, sub, ex_id, ext):
        return os.path.join(self.folder, sub, ex_id + ext)

    def add(self, jpeg, crop, mask, lid, size, colour, guesses, feats, setup=None):
        """Keep a named lid. With `lid` None (the camera found no lid) only the
        photo and the answer are kept: a missed lid, counted against its colour.
        `setup`: more fields for the record (marker size, empty bed)."""
        ex_id = time.strftime("%Y%m%d-%H%M%S") + "-%03d" % (int(time.time() * 1000) % 1000)
        rec = {"id": ex_id, "time": time.time(), "size": size, "colour": colour,
               "measured": None, "crop_ppm": CROP_PPM, "trim_mm": TRIM_MM}
        rec.update(setup or {})
        rec["guesses"] = guesses
        has_crop = lid is not None and crop is not None
        if has_crop:
            rec.update({"measured": [round(float(v), 2) for v in lid.size_mm],
                        "width_is_min": bool(lid.width_is_min),
                        "length_is_min": bool(lid.length_is_min), "method": lid.method})
        else:
            feats = None
        # The record first: if it cannot be written, no files are left behind
        # that nothing points to.
        line = json.dumps(rec) + "\n"
        if jpeg is not None:
            with open(self._p("photos", ex_id, ".jpg"), "wb") as f:
                f.write(jpeg)
        if has_crop:
            cv.imwrite(self._p("crops", ex_id, ".png"), crop)
            cv.imwrite(self._p("masks", ex_id, ".png"), mask)
        with open(self.index, "a") as f:
            f.write(line)
        ex = Example(rec)
        ex.features = feats
        self._save_features(ex)
        self.examples.append(ex)
        return ex

    def remeasure(self, ex, lid, crop, mask):
        """Measure a named lid again from its photo (a better empty-bed photo,
        a better outline). The answer and the guesses made at the time stay;
        the features are rebuilt from the new crop."""
        rec = ex.rec
        for sub, ext in (("crops", ".png"), ("masks", ".png"), ("features", ".npz")):
            try:
                os.remove(self._p(sub, ex.id, ext))
            except OSError:
                pass
        if lid is not None and crop is not None:
            cv.imwrite(self._p("crops", ex.id, ".png"), crop)
            cv.imwrite(self._p("masks", ex.id, ".png"), mask)
            rec.update({"measured": [round(float(v), 2) for v in lid.size_mm],
                        "width_is_min": bool(lid.width_is_min),
                        "length_is_min": bool(lid.length_is_min), "method": lid.method})
        else:
            rec.update({"measured": None, "width_is_min": False, "length_is_min": False})
            rec.pop("method", None)
        rec["remeasured"] = time.strftime("%Y-%m-%d %H:%M:%S")
        ex.__init__(rec)                    # measured, flags; features None: rebuilt
        self._rewrite()

    def photo_path(self, ex):
        return self._p("photos", ex.id, ".jpg")

    def _rewrite(self):
        tmp = self.index + ".tmp"
        with open(tmp, "w") as f:
            for e in self.examples:
                f.write(json.dumps(e.rec) + "\n")
        os.replace(tmp, self.index)

    def remove(self, ex_id):
        self.examples = [e for e in self.examples if e.id != ex_id]
        self._rewrite()
        for sub, ext in (("photos", ".jpg"), ("crops", ".png"), ("masks", ".png"),
                         ("features", ".npz")):
            try:
                os.remove(self._p(sub, ex_id, ext))
            except OSError:
                pass

    def _save_features(self, ex):
        if ex.features is not None:
            np.savez(self._p("features", ex.id, ".npz"), **ex.features)

    def load_features(self, network=None, progress=None):
        """Fill in every example's features, from the cache when it is current."""
        want_model = network.sha if network is not None else None
        for i, ex in enumerate(self.examples):
            if progress:
                progress(i, len(self.examples))
            if ex.features is not None or ex.measured is None:   # missed lids have no crop
                continue
            path = self._p("features", ex.id, ".npz")
            feats = None
            if os.path.exists(path):
                with np.load(path) as z:
                    feats = {k: z[k] for k in z.files}
                stale = int(feats.get("version", -1)) != FEATURE_VERSION
                have_model = str(feats["model"]) if "model" in feats else None
                if stale or (want_model and have_model != want_model):
                    feats = None
            if feats is None:
                crop = cv.imread(self._p("crops", ex.id, ".png"), cv.IMREAD_COLOR)
                mask = cv.imread(self._p("masks", ex.id, ".png"), cv.IMREAD_GRAYSCALE)
                if crop is None or mask is None:
                    continue
                feats = features(crop, mask, network)
                ex.features = feats
                self._save_features(ex)
            ex.features = feats


# ------------------------------------------------------------------ distances
def _colour_obj(f):
    c = LidVision.Colour.__new__(LidVision.Colour)
    c.median = np.asarray(f["colour_median"], np.float64).copy()
    c.hist = np.asarray(f["colour_hist"], np.float64).reshape(LidVision.Colour.BINS)
    return c


def _emd(p, q):
    def sig(rows):
        s = rows.astype(np.float32).copy()
        s[:, 1] *= LidVision.L_WEIGHT
        return s
    d, _lb, _flow = cv.EMD(sig(p), sig(q), cv.DIST_L2)
    return float(d)


def _raw_distance(kind, a, b, tstd=None):
    """Distance of one kind between two feature dicts, or None if either lacks it."""
    if kind == "colour":
        return LidVision.colour_distance(_colour_obj(a), _colour_obj(b))
    if kind == "palette":
        return _emd(a["palette"], b["palette"])
    if kind == "texture":
        z = (a["texture"] - b["texture"]) / tstd
        return float(np.sqrt(np.mean(z * z)))
    if kind == "network":
        if "embedding" not in a or "embedding" not in b:
            return None
        return float(1.0 - np.dot(a["embedding"], b["embedding"]))
    raise KeyError(kind)


# Which kinds of distance each method adds up (each scaled to its typical size).
METHOD_KINDS = {
    "colour": ("colour",),
    "palette": ("palette", "texture"),
    "network": ("network",),
    "all": ("network", "palette", "texture"),
}


class Memory:
    """The named lids, ready to compare a new one with.

    Built from a list of examples (in replay, only the ones named before the lid
    being guessed), so a guess never sees its own answer.
    """

    def __init__(self, examples, outlines, pair_cache=None):
        self.examples = [e for e in examples if e.features is not None]
        self.outlines = outlines            # {name: (w, l)}
        # Distances between two named lids never change (texture aside, which
        # is cheap): replay keeps them across its many Memories.
        self.pair_cache = {} if pair_cache is None else pair_cache
        tex = np.array([e.features["texture"] for e in self.examples]) if self.examples else None
        if tex is not None and len(tex) >= 3:
            self.tstd = np.maximum(tex.std(axis=0), 1e-3)
        else:
            self.tstd = np.ones(len(self.examples[0].features["texture"])) if self.examples else None
        self.scale = {k: self._typical(k) for k in ("colour", "palette", "texture", "network")}

    def _typical(self, kind):
        """Median distance between two named lids: what 'far apart' means."""
        n = len(self.examples)
        if n < 2:
            return 1.0
        rng = np.random.default_rng(n)
        pairs = SCALE_PAIRS if n * (n - 1) // 2 > SCALE_PAIRS else None
        if pairs is None:
            idx = [(i, j) for i in range(n) for j in range(i + 1, n)]
        else:
            idx = [tuple(rng.choice(n, 2, replace=False)) for _ in range(pairs)]
        ds = [self._between(kind, self.examples[i], self.examples[j]) for i, j in idx]
        ds = [d for d in ds if d is not None and d > 0]
        return float(np.median(ds)) if ds else 1.0

    def _raw(self, kind, a, b):
        return _raw_distance(kind, a, b, self.tstd)

    def _between(self, kind, ea, eb):
        """`_raw` between two named lids, cached by their ids."""
        if kind == "texture":
            return self._raw(kind, ea.features, eb.features)
        key = (kind,) + tuple(sorted((ea.id, eb.id)))
        if key not in self.pair_cache:
            self.pair_cache[key] = self._raw(kind, ea.features, eb.features)
        return self.pair_cache[key]

    def distance(self, method, a, b, ea=None, eb=None):
        """Scaled distance by `method`. With both Examples given, the parts
        that never change come from the cache."""
        total, used = 0.0, 0
        for kind in METHOD_KINDS[method]:
            d = self._between(kind, ea, eb) if ea is not None and eb is not None \
                else self._raw(kind, a, b)
            if d is None:
                continue
            total += d / self.scale[kind]
            used += 1
        return total / used if used else None

    def rank_colours(self, method, feats):
        """[(colour code, distance, probability)], likeliest first; [] when
        nothing is named yet or the method cannot run (no network)."""
        best = {}
        for e in self.examples:
            d = self.distance(method, feats, e.features)
            if d is None:
                continue
            if e.colour not in best or d < best[e.colour]:
                best[e.colour] = d
        if not best:
            return []
        tau = self._spread(method)
        logs = {c: -0.5 * (d / tau) ** 2 for c, d in best.items()}
        top = max(logs.values())
        w = {c: math.exp(v - top) for c, v in logs.items()}
        total = sum(w.values())
        out = [(c, best[c], w[c] / total) for c in best]
        out.sort(key=lambda r: r[1])
        return out

    def _spread(self, method):
        """How far a lid typically is from the nearest OTHER lid of its own
        colour: the width of a colour, for turning distances into confidence."""
        cache = getattr(self, "_spreads", None)
        if cache is None:
            cache = self._spreads = {}
        if method in cache:
            return cache[method]
        by_colour = {}
        for e in self.examples:
            by_colour.setdefault(e.colour, []).append(e)
        ds = []
        for group in by_colour.values():
            if len(group) < 2:
                continue
            group = group[-20:]                 # the latest: lighting drifts
            for e in group[-8:]:
                near = [self.distance(method, None, None, e, o)
                        for o in group if o is not e]
                near = [d for d in near if d is not None]
                if near:
                    ds.append(min(near))
        tau = float(np.median(ds)) if len(ds) >= 3 else 0.5
        cache[method] = max(tau, 0.05)
        return cache[method]

    def rank_sizes(self, method, measured, width_is_min=False, length_is_min=False):
        """[(lid name, error in sigmas, probability)], likeliest first.

        `outline` names every lid whose outline equals the best one's, joined
        with ' / ': the camera cannot tell those apart.
        """
        if measured is None:
            return []
        errs = {}
        if method == "outline":
            groups = {}
            for name, size in self.outlines.items():
                groups.setdefault(tuple(sorted(size)), []).append(name)
            for size, names in groups.items():
                errs[" / ".join(sorted(names))] = LidVision.size_error(
                    measured, size, length_is_min, width_is_min)
        else:
            for e in self.examples:
                if e.measured is None or e.length_is_min or e.width_is_min:
                    continue
                d = LidVision.size_error(measured, e.measured, length_is_min, width_is_min)
                if e.size not in errs or d < errs[e.size]:
                    errs[e.size] = d
        if not errs:
            return []
        logs = {k: -0.5 * v ** 2 for k, v in errs.items()}
        top = max(logs.values())
        w = {k: math.exp(v - top) for k, v in logs.items()}
        total = sum(w.values())
        out = [(k, errs[k], w[k] / total) for k in errs]
        out.sort(key=lambda r: r[1])
        return out


# ------------------------------------------------------------------ step 2
LEARN_FROM = 30         # named lids (with a lid found) before step 2 starts
REFIT_EVERY = 10
# Lids that need another named lid of their own colour to learn from: below
# this many, there is nothing to fit the weights to.
MIN_PAIRED = 10
COMPONENTS = ("colour", "palette", "texture", "network")
COMPONENT_NAMES = {"colour": "Colour", "palette": "Palette", "texture": "Pattern",
                   "network": "Network"}
# Pulls the weights gently toward equal, so a handful of lids cannot swing them.
WEIGHT_PRIOR = 0.01
EMBED_DIMS = 48         # the network's 1280 numbers, reduced (PCA) for the classifier
HIST_DIMS = 16
PROBE_ALPHAS = (0.003, 0.03, 0.3, 3.0)
PROBE_FOLDS = 3


def fit_point(n):
    """How many of the first `n` named lids step 2 learns from: a multiple of
    REFIT_EVERY, 0 before LEARN_FROM. The window and the report both use it,
    so they refit at the same lids and make the same guesses."""
    k = n // REFIT_EVERY * REFIT_EVERY
    return k if k >= LEARN_FROM else 0


def _pair_matrix(kind, mem):
    """Scaled `kind` distance between every two of `mem`'s lids, inf on the
    diagonal. Vectorised where the distance allows; the palette's EMD comes
    from the shared cache, so each pair is computed once per session."""
    ex = mem.examples
    n = len(ex)
    if kind == "colour":
        med = np.array([e.features["colour_median"] for e in ex], np.float64)
        med[:, 0] *= LidVision.L_WEIGHT
        dm = np.sqrt(np.maximum(((med[:, None, :] - med[None, :, :]) ** 2).sum(axis=2), 0))
        h = np.sqrt(np.array([e.features["colour_hist"] for e in ex], np.float64))
        hist_d = np.sqrt(np.maximum(0.0, 1.0 - h @ h.T)) * 40.0
        d = 0.65 * dm + 0.35 * hist_d       # LidVision.colour_distance, all pairs at once
    elif kind == "texture":
        z = np.array([e.features["texture"] for e in ex]) / mem.tstd
        sq = (z * z).sum(axis=1)
        d = np.sqrt(np.maximum(sq[:, None] + sq[None, :] - 2 * z @ z.T, 0) / z.shape[1])
    elif kind == "network":
        e_ = np.array([e.features["embedding"] for e in ex])
        d = 1.0 - e_ @ e_.T
    else:
        d = np.zeros((n, n))
        for i in range(n):
            for j in range(i + 1, n):
                d[i, j] = d[j, i] = mem._between(kind, ex[i], ex[j])
    d = d / mem.scale[kind]
    np.fill_diagonal(d, np.inf)
    return d


def _softmax_rows(s):
    s = s - s.max(axis=1, keepdims=True)
    e = np.exp(s)
    return e / e.sum(axis=1, keepdims=True)


class Learned:
    """What the named lids teach about telling colours apart (step 2).

    `weights`: {component: weight}, or None if there was too little to fit.
    `probe`: the classifier, or None. Fitted once, on the lids given.
    """

    def __init__(self, examples, outlines, pair_cache):
        from scipy.optimize import minimize
        self._minimize = minimize
        mem = Memory(examples, outlines, pair_cache)
        self.n = len(mem.examples)
        net = all("embedding" in e.features for e in mem.examples)
        self.components = [k for k in COMPONENTS if k != "network" or net]
        self.weights = self._fit_weights(mem)
        self.probe = self._fit_probe(mem.examples, net)

    # --------------------------------------------------- learned weights
    def _fit_weights(self, mem):
        """Weights that make each lid's nearest same-colour lid score above
        every other colour's nearest lid: a softmax over colours, fitted by
        maximum likelihood, every lid left out of its own comparison."""
        ex = mem.examples
        labels = [e.colour for e in ex]
        classes = sorted(set(labels))
        members = {c: [i for i, l in enumerate(labels) if l == c] for c in classes}
        samples = [i for i, l in enumerate(labels) if len(members[l]) >= 2]
        if len(samples) < MIN_PAIRED or len(classes) < 2:
            return None
        mats = {k: _pair_matrix(k, mem) for k in self.components}
        D = np.empty((len(samples), len(classes), len(self.components)))
        for ci, c in enumerate(classes):
            cols = members[c]
            for ki, k in enumerate(self.components):
                D[:, ci, ki] = mats[k][samples][:, cols].min(axis=1)
        D = np.minimum(D, 20.0)
        y = np.array([classes.index(labels[i]) for i in samples])
        onehot = np.eye(len(classes))[y]
        prior = np.zeros(len(self.components))

        def loss(theta):
            w = np.exp(theta)
            s = -(D @ w)
            p = _softmax_rows(s)
            top = s.max(axis=1, keepdims=True)
            lse = (top + np.log(np.exp(s - top).sum(axis=1, keepdims=True))).ravel()
            ce = float(np.mean(lse - s[np.arange(len(y)), y]))
            g_w = -np.einsum("sc,sck->k", p - onehot, D) / len(y)
            reg = WEIGHT_PRIOR * float(((theta - prior) ** 2).sum())
            return ce + reg, g_w * w + 2 * WEIGHT_PRIOR * (theta - prior)

        r = self._minimize(loss, prior.copy(), jac=True, method="L-BFGS-B",
                           bounds=[(-6.0, 6.0)] * len(prior))
        w = np.exp(r.x)
        # How often each lid's own colour came out on top, with these weights.
        self.weights_hit = float(np.mean(np.argmax(-(D @ w), axis=1) == y))
        return dict(zip(self.components, w.tolist()))

    def rank_weighted(self, mem, feats):
        """[(code, score, p)], likeliest first. The weights also set how
        sharp the probabilities are, so they are fitted, not guessed."""
        comps = [k for k in self.components if k != "network" or "embedding" in feats]
        best = {}
        for e in mem.examples:
            v = []
            for k in comps:
                d = mem._raw(k, feats, e.features)
                v.append(np.inf if d is None else d / mem.scale[k])
            v = np.array(v)
            cur = best.get(e.colour)
            best[e.colour] = v if cur is None else np.minimum(cur, v)
        if not best:
            return []
        w = np.array([self.weights[k] for k in comps])
        codes = list(best)
        score = np.array([float(np.minimum(best[c], 20.0) @ w) for c in codes])
        p = _softmax_rows(-score[None, :])[0]
        out = list(zip(codes, score.tolist(), p.tolist()))
        out.sort(key=lambda r: -r[2])
        return out

    # ------------------------------------------------------ the classifier
    def _raw_vector(self, f):
        pal = np.asarray(f["palette"], np.float64)
        top = np.zeros((2, 4))
        top[:min(2, len(pal))] = pal[:2]
        mean_lab = (pal[:, :1] * pal[:, 1:]).sum(axis=0)
        return (np.asarray(f.get("embedding", np.zeros(0)), np.float64),
                np.sqrt(np.asarray(f["colour_hist"], np.float64)),
                np.concatenate([np.asarray(f["colour_median"], np.float64), mean_lab,
                                top.ravel(), np.asarray(f["texture"], np.float64)]))

    def _vector(self, f, pr):
        emb, hist, rest = self._raw_vector(f)
        parts = []
        if pr["emb_basis"] is not None:
            parts.append((emb - pr["emb_mean"]) @ pr["emb_basis"])
        parts.append((hist - pr["hist_mean"]) @ pr["hist_basis"])
        parts.append(rest)
        x = np.concatenate(parts)
        return (x - pr["mean"]) / pr["std"]

    @staticmethod
    def _pca(X, dims):
        mean = X.mean(axis=0)
        _u, _s, vt = np.linalg.svd(X - mean, full_matrices=False)
        return mean, vt[:min(dims, len(vt))].T

    def _train_lr(self, X, y, n_classes, alpha):
        d = X.shape[1]
        onehot = np.eye(n_classes)[y]

        def loss(params):
            W = params[:d * n_classes].reshape(d, n_classes)
            b = params[d * n_classes:]
            s = X @ W + b
            p = _softmax_rows(s)
            top = s.max(axis=1, keepdims=True)
            lse = (top + np.log(np.exp(s - top).sum(axis=1, keepdims=True))).ravel()
            ce = float(np.mean(lse - s[np.arange(len(y)), y]))
            g = (p - onehot) / len(y)
            gW = X.T @ g + alpha * W
            return ce + 0.5 * alpha * float((W * W).sum()), np.concatenate([gW.ravel(), g.sum(axis=0)])

        r = self._minimize(loss, np.zeros(d * n_classes + n_classes), jac=True, method="L-BFGS-B",
                           options={"maxiter": 300})
        return r.x[:d * n_classes].reshape(d, n_classes), r.x[d * n_classes:]

    def _fit_probe(self, ex, net):
        labels = [e.colour for e in ex]
        classes = sorted(set(labels))
        if len(classes) < 2:
            return None
        raws = [self._raw_vector(e.features) for e in ex]
        pr = {"classes": classes, "emb_basis": None}
        if net:
            pr["emb_mean"], pr["emb_basis"] = self._pca(np.array([r[0] for r in raws]), EMBED_DIMS)
        pr["hist_mean"], pr["hist_basis"] = self._pca(np.array([r[1] for r in raws]), HIST_DIMS)
        pr["mean"], pr["std"] = 0.0, 1.0
        X = np.array([self._vector(e.features, pr) for e in ex])
        pr["mean"], pr["std"] = X.mean(axis=0), np.maximum(X.std(axis=0), 1e-6)
        X = (X - pr["mean"]) / pr["std"]
        y = np.array([classes.index(l) for l in labels])

        # Regularisation by cross-validation: the strength that best names
        # lids it was not fitted on. Folds by order, so later lids test
        # earlier ones the way the window does.
        fold = np.arange(len(y)) % PROBE_FOLDS
        best_alpha, best_hit = PROBE_ALPHAS[-1], -1.0
        for alpha in PROBE_ALPHAS:
            hits, total = 0, 0
            for f in range(PROBE_FOLDS):
                tr, te = fold != f, fold == f
                seen = set(y[tr].tolist())
                te = te & np.isin(y, list(seen))
                if not te.any():
                    continue
                W, b = self._train_lr(X[tr], y[tr], len(classes), alpha)
                hits += int((np.argmax(X[te] @ W + b, axis=1) == y[te]).sum())
                total += int(te.sum())
            hit = hits / total if total else 0.0
            if hit > best_hit + 1e-9:
                best_alpha, best_hit = alpha, hit
        pr["W"], pr["b"] = self._train_lr(X, y, len(classes), best_alpha)
        pr["alpha"], pr["cv_hit"] = best_alpha, best_hit
        return pr

    def rank_trained(self, feats):
        pr = self.probe
        if pr["emb_basis"] is not None and "embedding" not in feats:
            return []
        x = self._vector(feats, pr)
        p = _softmax_rows((x @ pr["W"] + pr["b"])[None, :])[0]
        out = [(c, 1.0 - float(q), float(q)) for c, q in zip(pr["classes"], p)]
        out.sort(key=lambda r: -r[2])
        return out

    def describe(self):
        """One line on what was learned, for the window and the report."""
        parts = []
        if self.weights is not None:
            parts.append("weights " + " · ".join(
                "%s %.2f" % (COMPONENT_NAMES[k], v) for k, v in self.weights.items()))
        if self.probe is not None:
            parts.append("classifier right on %d%% of held-out lids"
                         % round(100 * self.probe["cv_hit"]))
        return ("Learned from %d lids: " % self.n) + ("; ".join(parts) if parts else "not enough pairs yet")


class Trainer:
    """Step 2, kept fitted as lids are named. `get` returns the Learned for
    the lids named so far: fitted on the first fit_point(n) of them, and kept
    until the next REFIT_EVERY. Safe to call from several threads."""

    def __init__(self, outlines, pair_cache=None):
        self.outlines = outlines
        self.pair_cache = {} if pair_cache is None else pair_cache
        self.cache = {}
        self.lock = threading.Lock()

    def get(self, usable):
        """`usable`: the named lids with a lid found, oldest first."""
        k = fit_point(len(usable))
        if k == 0:
            return None
        key = tuple(e.id for e in usable[:k])
        with self.lock:
            if key not in self.cache:
                self.cache[key] = Learned(usable[:k], self.outlines, self.pair_cache)
            return self.cache[key]


def size_matches(guess, truth):
    """An outline guess 'A / B' is right when the true lid is one of them."""
    return guess is not None and truth in guess.split(" / ")


def rank_all(memory, feats, learned=None):
    """Every colour method's ranking, {method: [(code, distance, p)]}.

    A method that could not run (no network; step 2 not started) is left out,
    so it is not counted wrong. One that ran but had nothing to compare with
    is an empty list: a miss, as it should be.
    """
    out = {}
    if feats is None:
        return out
    for m in BASE_METHODS:
        if "network" in METHOD_KINDS[m] and "embedding" not in feats:
            continue
        out[m] = memory.rank_colours(m, feats)
    if learned is not None:
        if learned.weights is not None:
            out["weighted"] = learned.rank_weighted(memory, feats)
        if learned.probe is not None:
            out["trained"] = learned.rank_trained(feats)
    return out


def guesses(memory, feats, lid, learned=None, ranks=None):
    """Every method's top guess, as stored with an example: {method: [name, p]},
    plus `ran`: the methods that could run. `ranks`: rank_all's answer, if
    the caller already has it."""
    if ranks is None:
        ranks = rank_all(memory, feats, learned)
    out = {m: ([r[0][0], round(r[0][2], 3)] if r else None) for m, r in ranks.items()}
    out["ran"] = sorted(ranks)
    measured = lid.size_mm if lid is not None else None
    for m in SIZE_METHODS:
        r = memory.rank_sizes(m, measured, lid.width_is_min, lid.length_is_min) if lid else []
        out["size_" + m] = [r[0][0], round(r[0][2], 3)] if r else None
    if learned is not None:
        out["learned_from"] = learned.n
    return out


def load_outlines(path):
    with open(path) as f:
        lids = json.load(f)["lids"]
    return {d["name"]: (d["width"], d["length"]) for d in lids.values()}


# ------------------------------------------------------------------- replay
def replay(examples, outlines):
    """Guess every example from only the examples named before it, in order:
    what the camera would have said at the time. One dict per example:

      truth, size               the operator's colour and size
      seen                      named lids of that colour before this one
      <method>                  True / False (None: the method could not run)
      p_<method>                its confidence in its top guess
    """
    rows = []
    usable = [e for e in examples if e.features is not None]
    counts = {}
    cache = {}
    trainer = Trainer(outlines, cache)
    for i, ex in enumerate(usable):
        mem = Memory(usable[:i], outlines, cache)
        row = {"id": ex.id, "truth": ex.colour, "size": ex.size,
               "seen": counts.get(ex.colour, 0)}
        ranks = rank_all(mem, ex.features, trainer.get(usable[:i]))
        for m in COLOUR_METHODS:
            r = ranks.get(m)
            if r is None:
                row[m], row["p_" + m] = None, None
            else:
                row[m] = bool(r) and r[0][0] == ex.colour
                row["p_" + m] = r[0][2] if r else 0.0
        for m in SIZE_METHODS:
            r = mem.rank_sizes(m, ex.measured, ex.width_is_min, ex.length_is_min)
            row["size_" + m] = bool(r) and size_matches(r[0][0], ex.size) if m == "outline" \
                else bool(r) and r[0][0] == ex.size
        rows.append(row)
        counts[ex.colour] = counts.get(ex.colour, 0) + 1
    return rows, trainer


SEEN_BUCKETS = ((0, 0, "0"), (1, 1, "1"), (2, 2, "2"), (3, 3, "3"), (4, 4, "4"),
                (5, 9, "5-9"), (10, 10 ** 9, "10+"))


def by_seen(rows, method):
    """Accuracy by how many lids of the true colour had been named before:
    [(label, correct, total)]. The learning curve that matters - it shows how
    many named lids a colour needs, whatever order they came in."""
    out = []
    for lo, hi, label in SEEN_BUCKETS:
        sel = [r[method] for r in rows if lo <= r["seen"] <= hi and r.get(method) is not None]
        out.append((label, sum(1 for v in sel if v), len(sel)))
    return out
