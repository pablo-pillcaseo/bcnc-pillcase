"""Which of the lids waiting to be engraved is on the bed: the candidates, and
the odds of each.

The lid on the bed is one of the candidates: the scanned orders' lids not yet
engraved (picking is scan-confirmed, so a lid from outside them is not
expected). So this is a choice among a known few, made the Bayesian way:

  prior        how many of each are waiting: with three Navy Weekly Vitamins
               and one Pink Mission Pill left, 3 to 1 before anything is seen
  size         how far the measured lid is from the size this camera measures
               for that shape (learned from named lids; the outline until then)
  colour       how far it is from the colour's named lids (LidLearn: network +
               palette, unless another way has been clearly more often right;
               best_colour_method), or from its thumbnail while that colour
               has none

Candidates are grouped by what the camera can tell apart: the lid's physical
shape and its colour. Products whose lids are the same shape (Weekly Vitamin
and Weekly Vitamin XL) are one shape here, so a Navy lid of that shape is one
group holding every waiting row it could be engraved for, from any order.
Which of those rows it is - which text goes on it - stays the operator's pick.

"None of these" is only a warning, never a choice: it comes up when no
waiting lid is plausible (a lid of a size nothing waiting has, a colour
unlike all of them), which means an empty bed, a lid from elsewhere, or a
camera that has lost its markers.

Nothing here touches Tk, the serial port or the config: it runs on the
camera's worker thread, and in tests.
"""

import json
import math
import os

import numpy as np

import LidLearn
import LidVision

# Measured this many sigmas from the best group's size or colour: implausible.
IMPLAUSIBLE_Z = 3.5
# Named lids of a shape, not running off the view, before their measured size
# replaces the outline as what that shape measures here.
LEARNED_SIZE_MIN = 2
# A colour with no named lids yet is matched on its thumbnail, which is a
# product photo, not this camera: trusted this much less.
THUMBNAIL_SIGMA_SCALE = 2.0
# A colour with nothing to compare with (no named lids, no thumbnail) counts
# as this far off: between a typical right colour (about 1) and a typical
# wrong one (3 and more). At 0 it would beat every colour that has evidence.
UNKNOWN_COLOUR_Z = 2.0
# Recent tests (named lids whose colour had been named before) looked at to
# choose the colour method; tests each method needs before it is judged, and
# how much more often another must be right to replace the network.
METHOD_WINDOW = 50
METHOD_MIN = 20
METHOD_MARGIN = 0.10


# ------------------------------------------------------------------- shapes
def _outline_table():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lid_outlines.json")
    try:
        with open(path) as f:
            return json.load(f).get("lids", {})
    except (OSError, ValueError):
        return {}


_OUTLINES = None


def _outlines():
    global _OUTLINES
    if _OUTLINES is None:
        _OUTLINES = _outline_table()
    return _OUTLINES


def detection_candidates():
    """Every lid shape there is, as LidVision.identify candidates. Finding
    the lid on the bed uses these, never the waiting lids alone: the lids
    waiting must not decide what size of lid can be seen (the camera measures
    a lid against the fence narrower than its outline)."""
    return [{"key": code, "size": (rec["width"], rec["length"]), "colour_key": None,
             "lid": rec.get("name")} for code, rec in sorted(_outlines().items())]


def shape_key(size):
    """One key for every lid of this physical shape: its outline, short side
    first, to 0.1 mm."""
    if not size:
        return None
    w, l = sorted(float(v) for v in size)
    return "%.1fx%.1f" % (w, l)


def shape_of_name(name):
    """The shape key of a lid named as in lid_outlines.json ('Weekly Vitamin
    Case'), or None."""
    for rec in _outlines().values():
        if rec.get("name") == name:
            return shape_key((rec["width"], rec["length"]))
    return None


def shape_names(key):
    """The lids of a shape, by name: 'Weekly Vitamin Case / Weekly Vitamin XL Case'."""
    names = sorted(rec["name"] for rec in _outlines().values()
                   if shape_key((rec["width"], rec["length"])) == key)
    return " / ".join(names)


# --------------------------------------------------------------- candidates
class Group:
    """Waiting lids the camera cannot tell apart: one shape, one colour."""

    def __init__(self, shape, colour, size):
        self.shape = shape              # shape_key, or the lid name if no outline
        self.colour = colour            # colour code
        self.size = size                # (w, l) outline mm, or None
        self.members = []               # candidate dicts, best-known first

    @property
    def count(self):
        return len(self.members)

    @property
    def first(self):
        return self.members[0]


def group_candidates(candidates):
    """Candidates (dicts with at least 'size', 'colour_key', 'lid') -> [Group].
    A candidate without a known outline is its own shape, by lid name."""
    groups = {}
    for c in candidates:
        shape = shape_key(c.get("size")) or ("?" + str(c.get("lid") or c.get("key")))
        key = (shape, c.get("colour_key") or "")
        if key not in groups:
            groups[key] = Group(shape, key[1], c.get("size"))
        groups[key].members.append(c)
    return list(groups.values())


# ------------------------------------------------------------------- sizes
def learned_sizes(examples):
    """{shape: (w, l)}: what this camera measures for each shape, from named
    lids fully in view (the median, so one bad outline does not move it)."""
    by_shape = {}
    for e in examples:
        if e.measured is None or e.length_is_min or e.width_is_min:
            continue
        # Saved by bCNC with its shape; by the teaching tool, under a lid name.
        shape = e.rec.get("shape") or shape_of_name(e.size)
        if shape:
            by_shape.setdefault(shape, []).append(e.measured)
    return {s: tuple(np.median(np.array(v), axis=0).tolist())
            for s, v in by_shape.items() if len(v) >= LEARNED_SIZE_MIN}


# ------------------------------------------------------------------ colours
METHOD_NAMES = {
    "colour": "Colour",
    "palette": "Palette + pattern",
    "network": "Network",
    "all": "Network + palette",
    "weighted": "Learned weights",
    "trained": "Trained layer",
}


def method_ranking(examples, network=None):
    """How often each colour method's guess, made before the engraver named
    the lid, was the colour named: [(method, right, tested)] over the latest
    METHOD_WINDOW tests, best first. A lid whose colour had no named lids
    before it tests nothing: no method can name a colour it has never seen.
    Methods that need the network are left out without one."""
    seen = set()
    tests = []
    for e in examples:
        g = e.rec.get("guesses") or {}
        if e.measured is not None and g.get("ran") is not None and e.colour in seen:
            tests.append((e.colour, g))
        seen.add(e.colour)
    hits = {}
    for colour, g in tests[-METHOD_WINDOW:]:
        for m in g["ran"]:
            if m in LidLearn.COLOUR_METHODS and (
                    network is not None or "network" not in LidLearn.METHOD_KINDS.get(m, ())):
                v = g.get(m)
                hits.setdefault(m, []).append(bool(v) and v[0] == colour)
    rows = [(m, sum(v), len(v)) for m, v in hits.items()]
    rows.sort(key=lambda r: (-r[1] / r[2], -r[2]))
    return rows


def best_colour_method(examples, network):
    """The colour method to use, and its recent hit rate (None until tested).

    Network + palette (palette + pattern without the network) is used until
    it and another method have both been tested on METHOD_MIN lids, and is
    replaced only by a method right at least METHOD_MARGIN more often: a lucky
    run on a few lids does not swap the method under the engraver."""
    default = "all" if network is not None else "palette"
    rates = {m: (right / n, n) for m, right, n in method_ranking(examples, network)}
    base, base_n = rates.get(default, (None, 0))
    if base is None or base_n < METHOD_MIN:
        return default, base
    better = [m for m, (rate, n) in rates.items()
              if n >= METHOD_MIN and rate >= base + METHOD_MARGIN]
    if not better:
        return default, base
    m = max(better, key=lambda k: rates[k][0])
    return m, rates[m][0]


def learned_colour_loglik(memory, feats, learned, method):
    """{colour code: log-likelihood} for the colours `method` has named lids
    of. Distance methods give -z^2/2 in their own spread; the learned ones
    give their own calibrated scores."""
    if feats is None or not memory.examples:
        return {}
    if method == "weighted":
        if learned is None or learned.weights is None:
            return {}
        return {c: -score for c, score, _p in learned.rank_weighted(memory, feats)}
    if method == "trained":
        if learned is None or learned.probe is None:
            return {}
        return {c: math.log(max(p, 1e-9)) for c, _d, p in learned.rank_trained(feats)}
    if "network" in LidLearn.METHOD_KINDS[method] and "embedding" not in feats:
        return {}
    tau = memory._spread(method)
    return {c: -0.5 * (d / tau) ** 2 for c, d, _p in memory.rank_colours(method, feats)}


# ------------------------------------------------------------------ matching
class GroupMatch:
    def __init__(self, group, probability, size_z, colour_z, colour_from, notes):
        self.group = group
        self.probability = probability
        self.size_z = size_z            # sigmas from this shape's size, or None
        self.colour_z = colour_z        # sigmas from this colour, or None
        self.colour_from = colour_from  # "named lids", "thumbnail" or None
        self.notes = notes


class Match:
    """The outcome for one photo: every group with its odds, best first."""

    def __init__(self, groups, warnings, method, method_hit):
        self.groups = groups            # [GroupMatch]
        self.warnings = warnings
        self.method = method            # the colour method used
        self.method_hit = method_hit    # its recent hit rate, or None

    @property
    def best(self):
        return self.groups[0] if self.groups else None


def match(res, candidates, feats=None, memory=None, learned=None, examples=(),
          reference_colour=None, method="all", method_hit=None):
    """Odds of each waiting group for one photo's LidVision result `res`.

    `feats`: LidLearn features of the lid's crop. `memory` / `learned`: the
    named lids (LidLearn.Memory) and step 2 (LidLearn.Learned or None).
    `reference_colour(code)`: a LidVision.Colour for a colour with no named
    lids (thumbnail, or ColourMemory), or None.
    """
    groups = group_candidates(candidates)
    warnings = []
    if not groups:
        return Match([], ["Nothing is waiting to be engraved."], method, method_hit)

    sizes = learned_sizes(examples)
    lid = res.lid
    colour_ll = learned_colour_loglik(memory, feats, learned, method) if memory else {}
    scored = []
    for g in groups:
        notes = []
        log_p = math.log(g.count)
        size_z = None
        if lid is not None and g.size:
            want = sizes.get(g.shape)
            if want is None:
                want = g.size
            else:
                notes.append("size as measured here")
            size_z = LidVision.size_error(lid.size_mm, want, lid.length_is_min, lid.width_is_min)
            log_p -= 0.5 * size_z ** 2
        colour_z, colour_from = None, None
        if g.colour in colour_ll:
            ll = colour_ll[g.colour]
            log_p += ll
            colour_z = math.sqrt(max(0.0, -2.0 * ll)) if method not in ("weighted", "trained") else None
            colour_from = "named lids"
        elif res.colour is not None and reference_colour is not None:
            ref = reference_colour(g.colour)
            if ref is not None:
                colour_z = LidVision.colour_distance(res.colour, ref) / (
                    LidVision.COLOUR_SIGMA * THUMBNAIL_SIGMA_SCALE)
                log_p -= 0.5 * colour_z ** 2
                colour_from = "thumbnail"
        if colour_from is None:
            colour_z = UNKNOWN_COLOUR_Z
            log_p -= 0.5 * colour_z ** 2
            notes.append("colour not compared: nothing to compare with yet")
        scored.append([g, log_p, size_z, colour_z, colour_from, notes])

    top = max(s[1] for s in scored)
    weights = [math.exp(s[1] - top) for s in scored]
    total = sum(weights)
    out = [GroupMatch(s[0], w / total, s[2], s[3], s[4], s[5]) for s, w in zip(scored, weights)]
    out.sort(key=lambda m: -m.probability)

    best = out[0]
    if lid is None:
        warnings.append("No lid found on the bed: the odds are only how many of each are waiting.")
    elif best.size_z is not None and best.size_z > IMPLAUSIBLE_Z:
        warnings.append("No waiting lid is this size (measured %s): is it the right lid, "
                        "fully in the fixture?" % LidVision.describe_size(lid))
    if best.colour_z is not None and best.colour_z > IMPLAUSIBLE_Z:
        warnings.append("Its colour is unlike every waiting lid's.")
    return Match(out, warnings, method, method_hit)


def describe_group(g):
    """'Weekly Vitamin Case / Weekly Vitamin XL Case - Navy Blue'."""
    names = shape_names(g.shape) if g.size else ""
    first = g.first.get("row") or {}
    case = names or first.get("case_type") or g.first.get("lid") or "?"
    return "%s - %s" % (case, first.get("colour_name") or g.colour or "?")
