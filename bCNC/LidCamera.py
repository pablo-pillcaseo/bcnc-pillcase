"""Lid camera for Tote Scanning: photograph the bed, pick the lid, confirm.

After a tote is scanned (or on "Identify Lid"), the camera on the BLTouch board
takes a photo of the work area. LidVision finds and measures the lid, and
LidMatch gives the odds of each lid waiting to be engraved (the candidate
pool, below). A dialog shows the photo and every waiting lid, likeliest first.
The operator confirms, which starts the engraving (Engrave Selected, then
Align & Run), or picks another, or retakes the photo.

Every confirmation teaches the camera (LidLearn): the lid's photo and crop are
kept as a named lid, labelled by the scan-verified order, and the bare fixture
round it updates the empty-bed map. A lid engraved by hand soon after a photo
teaches it too, so a correction made outside the dialog is not lost.

The candidate pool. Today it is the tote on show. It can also be every order
scanned on this station that still has lids waiting (setting "candidates"),
and it is built so that a single scanning point can later feed it every
scanned order: picking a lid from another order loads that order and selects
its row.

Nothing here can delay probing:
  * No photo is taken while the machine runs, Align & Run is probing, or Auto
    Setup is working. The check is made before each photo.
  * The capture and the vision run on a worker thread. The Tk thread only
    polls for the result, so the window never freezes either.
  * The capture shares the probe port's lock with the servo commands (see
    Sender.blt_camera_capture and BltCamera).

Per-machine files, next to the bCNC ini:
  <ini>-lidcam-last.jpg               the last photo, for checking what the camera sees
  <ini>-lidcam-examples/              named lids (LidLearn.Store): photos, crops, answers
  <ini>-lidcam-bedmap.png/-count.png/.json   the empty-bed map (LidVision.BedMap)
  <ini>-lidcam-background.png/.json   an older single empty-bed photo, read if no map
  <ini>-lidcam-corner.json            the fixture corner lids are pushed into
  <ini>-lidcam-colours.json           colours learned from confirmed lids (fallback)
  <ini>-lidcam-exposure.json          the fixed exposure (Calibrate Exposure)
  <ini>-lidcam-models/                the image network, once fetched
"""

import json
import os
import shutil
import threading
import time
from tkinter import (
    BOTH, BOTTOM, END, LEFT, RIGHT, TOP, W, X, Y, YES,
    BooleanVar, Button, Checkbutton, Entry, Frame, Label, LabelFrame, Radiobutton,
    Scrollbar, StringVar, Text, Toplevel, messagebox, ttk,
)

import BltCamera
import EngravingSession as ES
import LidLearn
import LidMatch
import LidVision
import PillcaseOrder
import Utils
from Utils import _

_SECTION = "LidCamera"
POLL_MS = 100
PREVIEW_WIDTH = 640

# Below this a confirm asks twice: the camera is not sure, so the operator
# should be.
LOW_CONFIDENCE = 0.5
# A lid engraved by hand within this long of a photo is taken as what the
# photo showed, and teaches the camera.
TEACH_BY_HAND_S = 300
# Orders kept in the pool with lids waiting, at most this long after scanning.
ORDER_KEEP_H = 24
# Where the teaching tool (tools/lidcam_learn.py) keeps its lids.
TEACHING_DIR = os.path.expanduser("~/lidcam")


def _path(suffix):
    return Utils.iniUser + "-lidcam-" + suffix


def enabled():
    return Utils.getBool(_SECTION, "enabled", True)


def auto_after_scan():
    return Utils.getBool(_SECTION, "autoAfterScan", True)


def candidate_scope():
    """'tote': the tote on show; 'all': every order scanned here with lids waiting."""
    v = Utils.getStr(_SECTION, "candidates", "tote")
    return v if v in ("tote", "all") else "tote"


def marker_mm():
    v = Utils.getFloat(_SECTION, "markerMM", LidVision.DEFAULT_MARKER_MM)
    return v if v > 0 else LidVision.DEFAULT_MARKER_MM


def size_scale():
    v = Utils.getFloat(_SECTION, "sizeScale", 1.0)
    return v if 0.5 < v < 2.0 else 1.0


# ------------------------------------------------------- empty bed, corner
def load_bed():
    """The empty-bed map, or one made from an older single empty-bed photo."""
    try:
        return LidVision.load_bed(_path("bedmap"), _path("background"))
    except Exception:
        return None


def clear_bed():
    for name in ("bedmap.png", "bedmap-count.png", "bedmap.json",
                 "background.png", "background.json"):
        try:
            os.remove(_path(name))
        except OSError:
            pass


def load_corner():
    try:
        with open(_path("corner.json")) as f:
            return LidVision.LidCorner.from_dict(json.load(f))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_corner(corner):
    with open(_path("corner.json"), "w") as f:
        json.dump(corner.to_dict(), f, indent=1)


def model_path():
    """The image network's file, wherever it was fetched to, or None."""
    for folder in (_path("models"), os.path.join(TEACHING_DIR, "models")):
        p = os.path.join(folder, LidLearn.MODEL_FILE)
        if os.path.exists(p):
            return p
    return None


# ------------------------------------------------------------ the pool
class CandidatePool:
    """The lids waiting to be engraved that the camera chooses among.

    Orders come in as they are scanned (`add_order`). The tote on show is
    always in; with scope 'all', so is every other order scanned here within
    ORDER_KEEP_H that still has lids waiting. Which lids are waiting comes from
    the shared progress store, so a lid engraved on another station drops out.
    """

    def __init__(self, frame):
        self.frame = frame
        self.orders = {}            # tote_key -> {"scan", "mode", "rows", "row_keys", "added"}

    def add_order(self, tote_key, scan, mode, rows, row_keys):
        if tote_key:
            self.orders[tote_key] = {"scan": scan, "mode": mode, "rows": list(rows),
                                     "row_keys": list(row_keys), "added": time.time()}

    def order(self, tote_key):
        return self.orders.get(tote_key)

    def _lid_defaults(self):
        gen = self.frame._gen()
        if not hasattr(gen, "_lid_defaults"):
            gen._lid_defaults = gen._load_lid_defaults()
        return gen._lid_defaults

    def candidates(self, scope):
        f = self.frame
        defaults = self._lid_defaults()
        now = time.time()
        keys = [f.tote_key] if f.tote_key else []
        if scope == "all":
            keys += [k for k, o in self.orders.items()
                     if k != f.tote_key and now - o["added"] < ORDER_KEEP_H * 3600]
        out = []
        for tk in keys:
            current = tk == f.tote_key
            o = {"scan": f.scan_value, "mode": f.search_mode, "rows": f.rows,
                 "row_keys": f.row_keys} if current else self.orders[tk]
            done = f.progress.completed(tk)
            for i, row in enumerate(o["rows"]):
                if not ES.has_engraving(row) or o["row_keys"][i] in done:
                    continue
                if current:
                    lid = f._lid_for(i)
                else:
                    hits = PillcaseOrder.lids_for_product(defaults, row.get("product_code"))
                    lid = hits[0] if hits else None
                out.append({
                    "key": "%s|%s" % (tk, o["row_keys"][i]),
                    # The lid as it is, not its engraving frame (see lid_outline).
                    "size": PillcaseOrder.lid_outline(defaults.get(lid)) if lid else None,
                    "colour_key": (row.get("colour_code") or "").upper(),
                    "lid": lid, "row": row, "row_index": i, "row_key": o["row_keys"][i],
                    "tote_key": tk, "scan": o["scan"], "mode": o["mode"], "current": current,
                })
        # The tote on show first: among lids that look alike, its rows lead.
        out.sort(key=lambda c: not c["current"])
        return out


class Learning:
    """The named lids and what they teach. Loaded on the camera's worker
    thread on first use (building features takes a moment), then kept."""

    def __init__(self):
        self.lock = threading.Lock()
        self.network = None
        p = model_path()
        if p:
            try:
                self.network = LidLearn.Network(p)
            except Exception as e:
                print("[lidcam] image network not used:", e)
        self.store = LidLearn.Store(_path("examples"))
        self.store.load_features(self.network)
        self.outlines = LidLearn.load_outlines(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "lid_outlines.json"))
        self.pair_cache = {}
        self.trainer = LidLearn.Trainer(self.outlines, self.pair_cache)

    def usable(self):
        return [e for e in self.store.examples if e.features is not None]


# ========================================================== the controller
class Controller:
    """The camera side of an EngravingFrame (`frame`)."""

    def __init__(self, frame):
        self.frame = frame
        self.app = frame.app
        self.colours = LidVision.ColourMemory(_path("colours.json"))
        self.pool = CandidatePool(frame)
        self._thumb_colours = {}
        self._job = None            # worker in flight: {"thread", "result", "error", "done"}
        self._dialog = None
        self._learning = None       # Learning, once loaded
        self._learning_lock = threading.Lock()
        self._bed = None            # the empty-bed map in use (swapped whole when changed)
        self._bed_loaded = False
        self._last = None           # the last photo's outcome, until taught or replaced

    # ============================================================ guards
    def why_not(self):
        """Why a photo cannot be taken right now, or None if it can."""
        if not LidVision.available():
            return _("The lid camera needs OpenCV 4.7 or newer (pip install -r requirements.txt).")
        if self.app.blt_serial is None:
            return _("The probe is not connected - connect it (or run Auto Setup) first.")
        if self.app.blt_has_camera is False:
            return _("The probe board has no camera firmware.")
        page = self.frame._page()
        if self.app.running or (page is not None and page.busy()):
            return _("The machine is running - the camera waits until it stops.")
        try:
            from CNCRibbon import Page
            setup = getattr(Page.frames.get("AutoSetup"), "setup", None)
            if setup is not None and setup.active:
                return _("Auto Setup is running.")
        except Exception:
            pass
        if self._job is not None:
            return _("A photo is already being taken.")
        return None

    # ===================================================== taking a photo
    def _start(self, work, on_done, on_error):
        """Run `work()` on a worker thread; call back on the Tk thread."""
        job = {"result": None, "error": None, "done": False}

        def run():
            try:
                job["result"] = work()
            except Exception as e:
                job["error"] = e
            job["done"] = True

        def poll():
            if not job["done"]:
                self.frame.after(POLL_MS, poll)
                return
            self._job = None
            if job["error"] is not None:
                on_error(job["error"])
            else:
                on_done(job["result"])

        self._job = job
        threading.Thread(target=run, daemon=True).start()
        self.frame.after(POLL_MS, poll)

    def _photo(self):
        """Worker thread: one capture, also kept on disk for diagnosis."""
        t0 = time.time()
        shot = self.app.blt_camera_capture()
        try:
            with open(_path("last.jpg"), "wb") as f:
                f.write(shot.jpeg)
        except OSError:
            pass
        print("[lidcam] photo %dx%d, %d bytes in %.2fs"
              % (shot.width, shot.height, len(shot.jpeg), time.time() - t0))
        return shot

    def learning(self):
        """The named lids, loaded on first use (from any thread)."""
        with self._learning_lock:
            if self._learning is None:
                self._learning = Learning()
            return self._learning

    def bed(self):
        if not self._bed_loaded:
            self._bed = load_bed()
            self._bed_loaded = True
        return self._bed

    def _set_bed(self, bed):
        """A changed map: saved, then swapped in whole."""
        if bed is None:
            clear_bed()
        else:
            bed.save(_path("bedmap"))
        self._bed, self._bed_loaded = bed, True

    # ========================================================= candidates
    def note_order(self):
        """The frame just loaded an order: it joins the pool."""
        f = self.frame
        self.pool.add_order(f.tote_key, f.scan_value, f.search_mode, f.rows, f.row_keys)

    def _candidates(self):
        return self.pool.candidates(candidate_scope())

    def _reference_colour(self, code, colour_name=None):
        """How a colour should look when it has no named lids: learned from
        this camera's confirmations, else the colour's thumbnail."""
        learned = self.colours.get(code)
        if learned is not None:
            return learned
        if code not in self._thumb_colours:
            path = PillcaseOrder.find_thumbnail(
                PillcaseOrder.thumbnail_dirs(Utils.getStr("SurfAlign", "thumbnailsDir", "")),
                code, colour_name or PillcaseOrder.colour_name_of(code))
            self._thumb_colours[code] = LidVision.colour_of_thumbnail(path) if path else None
        return self._thumb_colours[code]

    # ============================================================ identify
    def after_scan(self):
        """A tote was just listed: identify its lid if set up to, quietly."""
        if not (enabled() and auto_after_scan()):
            return
        if not self._candidates():
            return
        if self.app.blt_serial is None or self.app.blt_has_camera is False:
            return      # no camera here: scanning works exactly as before
        if self.why_not() is None:
            self.identify(auto=True)

    def identify(self, auto=False):
        reason = self.why_not()
        if reason:
            if not auto:
                self.frame._warn(reason)
            return
        candidates = self._candidates()
        if not candidates:
            if not auto:
                self.frame._warn(_("Scan a tote first - there are no lids waiting to match."))
            return
        self.frame._status(_("📷 Looking at the bed..."))
        bed, corner = self.bed(), load_corner()
        mm, scale = marker_mm(), size_scale()
        # Looked up here, on the Tk thread: the worker touches no config or rows.
        refs = {}
        for c in candidates:
            if c["colour_key"] not in refs:
                refs[c["colour_key"]] = self._reference_colour(c["colour_key"],
                                                               c["row"].get("colour_name"))

        def work():
            shot = self._photo()
            return self._analyse(shot, candidates, refs, bed, corner, mm, scale)

        def failed(e):
            msg = _("Lid camera: %s") % e
            if auto:
                # A scan must never be held up by the camera.
                self.frame._status(msg)
            else:
                self.frame._status(msg, error=True)
                self.frame._warn(msg)

        self._start(work, lambda out: self._show(out, auto), failed)

    def _analyse(self, shot, candidates, refs, bed, corner, mm, scale):
        """Worker thread: find the lid, describe it, and give the odds."""
        L = self.learning()
        res = LidVision.identify(shot.jpeg, LidMatch.detection_candidates(), lambda k: None,
                                 mm, scale, bed, corner)
        crop = mask = feats = None
        if res.lid is not None:
            crop, mask = LidLearn.lid_crop(res.rectified, res.lid, res.ppm)
            feats = LidLearn.features(crop, mask, L.network)
        with L.lock:
            examples = list(L.store.examples)
            usable = [e for e in examples if e.features is not None]
            memory = LidLearn.Memory(usable, L.outlines, L.pair_cache)
            learned = L.trainer.get(usable)
            method, hit = LidMatch.best_colour_method(examples, L.network)
            match = LidMatch.match(res, candidates, feats, memory, learned, examples,
                                   refs.get, method, hit)
            guesses = LidLearn.guesses(memory, feats, res.lid, learned)
        return {"shot": shot, "res": res, "match": match, "crop": crop, "mask": mask,
                "feats": feats, "guesses": guesses, "time": time.time(), "taught": False,
                "empty_bed": bed is not None}

    # ============================================================ teaching
    def _teach(self, out, shape, colour, lid_name, tote_key=None, row_key=None):
        """The photo in `out` showed this lid: keep it as a named lid, and let
        the bare fixture round it update the empty-bed map."""
        if out is None or out.get("taught"):
            return
        out["taught"] = True
        res = out["res"]
        L = self.learning()
        try:
            with L.lock:
                L.store.add(out["shot"].jpeg, out["crop"], out["mask"], res.lid, lid_name or "?",
                            colour, out["guesses"], out["feats"],
                            setup={"marker_mm": marker_mm(), "empty_bed": out["empty_bed"],
                                   "shape": shape, "source": "bcnc",
                                   "tote_key": tote_key, "row_key": row_key})
        except Exception as e:
            print("[lidcam] could not keep the named lid:", repr(e))
        bed = self.bed()
        if bed is not None and res.lid is not None:
            new = bed.copy()
            if new.learn(res):
                self._set_bed(new)
        if colour and res.colour is not None and res.lid is not None:
            self.colours.learn(colour, res.colour)

    def note_engraving(self, i):
        """The frame is about to engrave row `i`, picked by hand: if a photo
        was taken just before and not confirmed, it showed this lid."""
        out = self._last
        if out is None or out.get("taught") or time.time() - out["time"] > TEACH_BY_HAND_S:
            return
        f = self.frame
        row = f.rows[i]
        lid = f._lid_for(i)
        size = PillcaseOrder.lid_outline(self.pool._lid_defaults().get(lid)) if lid else None
        self._teach(out, LidMatch.shape_key(size), (row.get("colour_code") or "").upper(), lid,
                    f.tote_key, f.row_keys[i])

    # ============================================================== dialog
    def _select_row(self, member):
        """Select a candidate's row in the list, loading its order if it is
        not the one on show. Returns its row index, or None."""
        f = self.frame
        if member["tote_key"] != f.tote_key:
            o = self.pool.order(member["tote_key"])
            if o is None:
                return None
            f.load_order(o["scan"], o["mode"], o["rows"], identify=False)
        i = member["row_index"]
        iid = str(i)
        if f.tree.exists(iid):
            f.tree.selection_set(iid)
            f.tree.focus(iid)
            f.tree.see(iid)
        return i

    def _show(self, out, auto):
        f = self.frame
        if self._last is not None and not self._last.get("taught"):
            pass        # replaced: an unconfirmed photo teaches nothing
        self._last = out
        res, match = out["res"], out["match"]
        best = match.best
        if best is None or (auto and res.lid is None):
            # Scanned before the lid went in, most likely. After a scan that is
            # normal, so it is only mentioned; asked for, it is shown.
            f._status(_("Lid camera: no lid found. %s") % " ".join(res.warnings + match.warnings),
                      error=not auto)
            if not auto:
                self._dialog_for(out, None)
            return
        if best.group.first["current"]:
            self._select_row(best.group.first)
        f._status(_("📷 Looks like %s - %d%% sure. Confirm to engrave.")
                  % (LidMatch.describe_group(best.group), round(best.probability * 100)))
        self._dialog_for(out, best)

    def _dialog_for(self, out, chosen):
        if self._dialog is not None:
            try:
                self._dialog.destroy()
            except Exception:
                pass
        res, match = out["res"], out["match"]
        f = self.frame
        d = self._dialog = Toplevel(f)
        d.title(_("Lid Camera"))
        d.transient(f.winfo_toplevel())
        body = Frame(d, padx=12, pady=10)
        body.pack(fill=BOTH, expand=YES)

        top = Frame(body)
        top.pack(side=TOP, fill=X)
        img_lbl = Label(top)
        img_lbl.pack(side=LEFT, anchor="n")
        self._set_photo(img_lbl, res.annotated)

        side = Frame(top, padx=10)
        side.pack(side=LEFT, fill=BOTH, expand=YES)
        state = {"match": chosen, "member": None}
        head = StringVar()
        conf = StringVar()
        detail = StringVar()
        Label(side, textvariable=head, font=("", 14, "bold"), anchor=W, justify=LEFT,
              wraplength=420).pack(side=TOP, fill=X)
        conf_lbl = Label(side, textvariable=conf, font=("", 12, "bold"), anchor=W)
        conf_lbl.pack(side=TOP, fill=X)
        Label(side, textvariable=detail, anchor=W, justify=LEFT, wraplength=420).pack(
            side=TOP, fill=X, pady=(2, 4))
        warnings = res.warnings + match.warnings
        if warnings:
            Label(side, text="\n".join(warnings), fg="red", anchor=W, justify=LEFT,
                  wraplength=420, font=("", 9)).pack(side=TOP, fill=X)

        # Which of the group's rows: lids that look the same differ only in
        # their text (and order), which is the operator's call.
        rows_box = LabelFrame(side, text=_("Engrave which?"), padx=4, pady=4)
        row_var = StringVar()
        row_pick = ttk.Combobox(rows_box, textvariable=row_var, state="readonly", width=48)
        row_pick.pack(side=TOP, fill=X)

        def member_label(c):
            text = c["row"].get("engraving") or ""
            where = "" if c["current"] else _("  (order %s)") % c["scan"]
            return "%s%s" % (text, where)

        def pick_member(event=None):
            g = state["match"]
            if g is None:
                return
            idx = row_pick.current()
            state["member"] = g.group.members[max(0, idx)]

        row_pick.bind("<<ComboboxSelected>>", pick_member)

        def show(gm):
            state["match"] = gm
            if gm is None:
                head.set(_("No lid recognised"))
                conf.set("")
                detail.set(_("Place the lid in the fixture where the camera can see it and "
                             "the markers, then Retake."))
                rows_box.pack_forget()
                return
            g = gm.group
            head.set(LidMatch.describe_group(g))
            p = gm.probability
            conf.set(_("Confidence: %d%%") % round(p * 100))
            conf_lbl.config(fg="#1a7f37" if p >= 0.8 else "#b26b00" if p >= LOW_CONFIDENCE else "red")
            lines = []
            if res.lid is not None:
                lines.append(_("Measured %s") % LidVision.describe_size(res.lid)
                             + ((_(", lid is %.1f x %.1f mm") % tuple(sorted(g.size))) if g.size else ""))
            how = []
            if gm.size_z is not None:
                how.append(_("size %.1fσ off") % gm.size_z)
            if gm.colour_from:
                how.append(_("colour %s (%s)") % (
                    "%.1fσ off" % gm.colour_z if gm.colour_z is not None else _("scored"),
                    _("named lids") if gm.colour_from == "named lids" else _("thumbnail")))
            if how:
                lines.append(", ".join(how))
            lines += gm.notes
            detail.set("\n".join(lines))
            labels = [member_label(c) for c in g.members]
            row_pick.config(values=labels)
            row_pick.current(0)
            state["member"] = g.members[0]
            rows_box.pack(side=TOP, fill=X, pady=(4, 0))
            if g.first["current"]:
                self._select_row(g.first)

        # Every waiting lid, likeliest first.
        lst = LabelFrame(body, text=_("Waiting lids, likeliest first"), padx=4, pady=4)
        lst.pack(side=TOP, fill=BOTH, expand=YES, pady=(8, 0))
        cols = ("odds", "lid", "colour", "waiting")
        # Rows as tall as the text: the default height clips larger fonts.
        try:
            from tkinter import font as tkfont
            line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
            ttk.Style().configure("LidCam.Treeview", rowheight=line + 8)
        except Exception:
            pass
        tv = ttk.Treeview(lst, columns=cols, show="headings", height=min(8, max(3, len(match.groups))),
                          selectmode="browse", style="LidCam.Treeview")
        for c, title, w in (("odds", _("Odds"), 60), ("lid", _("Lid"), 440),
                            ("colour", _("Colour"), 160), ("waiting", _("Waiting"), 70)):
            tv.heading(c, text=title)
            tv.column(c, width=w, anchor="e" if c in ("odds", "waiting") else "w")
        sb = Scrollbar(lst, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side=LEFT, fill=BOTH, expand=YES)
        sb.pack(side=RIGHT, fill=Y)
        for k, gm in enumerate(match.groups):
            g = gm.group
            names = (LidMatch.shape_names(g.shape) if g.size else "") or g.first.get("lid") \
                or g.first["row"].get("case_type") or g.first["row"].get("product_code") or "?"
            tv.insert("", END, iid=str(k), values=(
                "%d%%" % round(gm.probability * 100), names,
                g.first["row"].get("colour_name") or g.colour, g.count))

        def on_pick(event=None):
            sel = tv.selection()
            if sel:
                show(match.groups[int(sel[0])])

        tv.bind("<<TreeviewSelect>>", on_pick)
        if match.method:
            Label(body, fg="gray", font=("", 8), anchor=W, text=_(
                "Colour judged by: %s%s. %d named lids.") % (
                match.method, "" if match.method_hit is None else
                _(" (right %d%% of the time lately)") % round(100 * match.method_hit),
                len(self._learning.store.examples) if self._learning else 0)).pack(side=TOP, fill=X)

        bar = Frame(body)
        bar.pack(side=TOP, fill=X, pady=(10, 0))

        def teach_pick():
            gm, member = state["match"], state["member"]
            if gm is None or member is None:
                return None
            self._teach(out, gm.group.shape, gm.group.colour, member["lid"],
                        member["tote_key"], member["row_key"])
            return member

        def close():
            d.destroy()
            self._dialog = None

        def confirm():
            gm = state["match"]
            if gm is None:
                return
            if gm.probability < LOW_CONFIDENCE and not messagebox.askyesno(
                    _("Low Confidence"),
                    _("The camera is only %d%% sure this is %s.\n\nEngrave it anyway?")
                    % (round(gm.probability * 100), LidMatch.describe_group(gm.group)), parent=d):
                return
            member = teach_pick()
            close()
            if member is not None and self._select_row(member) is not None:
                self._engrave()

        def select_only():
            member = teach_pick()
            close()
            if member is not None:
                self._select_row(member)

        def retake():
            close()
            self.identify()

        go = Button(bar, text=_("✓ Confirm & Engrave"), command=confirm, bg="#4CAF50", fg="white",
                    activebackground="#45a049", activeforeground="white", font=("", 12, "bold"))
        go.pack(side=LEFT, fill=X, expand=YES, padx=(0, 3))
        if not match.groups:
            go.config(state="disabled")
        Button(bar, text=_("Select Only"), command=select_only).pack(side=LEFT, padx=3)
        Button(bar, text=_("Retake"), command=retake).pack(side=LEFT, padx=3)
        Button(bar, text=_("Cancel"), command=close).pack(side=LEFT, padx=(3, 0))

        if chosen is not None:
            k = match.groups.index(chosen)
            tv.selection_set(str(k))
            tv.see(str(k))
        show(chosen)
        d.bind("<Escape>", lambda e: close())
        f._gen()._fit_and_center(d)
        go.focus_set()

    def _engrave(self):
        """Confirmed: Engrave Selected, then Align & Run."""
        f = self.frame
        if not f.prepare_selected():
            return
        page = f._page()
        multipoint = getattr(page, "multipoint", None) if page is not None else None
        if multipoint is None:
            f._status(_("Ready - press Align & Run."))
            return
        multipoint.quick_align_run()

    @staticmethod
    def _set_photo(label, bgr):
        if bgr is None:
            return
        try:
            import cv2 as cv
            from PIL import Image, ImageTk
        except ImportError:
            return
        h, w = bgr.shape[:2]
        scale = PREVIEW_WIDTH / float(w)
        small = cv.resize(bgr, (PREVIEW_WIDTH, int(h * scale)), interpolation=cv.INTER_AREA)
        photo = ImageTk.PhotoImage(Image.fromarray(cv.cvtColor(small, cv.COLOR_BGR2RGB)))
        label.config(image=photo)
        label.image = photo     # Tk drops an image nobody holds

    # ======================================================= teaching tool
    def import_teaching(self, src=os.path.join(TEACHING_DIR, "examples")):
        """Bring in the lids named in tools/lidcam_learn.py, and its fixture
        corner and empty-bed map where this machine has none. Returns how many
        lids."""
        tool = os.path.dirname(src)
        if load_corner() is None and os.path.exists(os.path.join(tool, "lid-corner.json")):
            shutil.copy2(os.path.join(tool, "lid-corner.json"), _path("corner.json"))
        if self.bed() is None:
            bed = LidVision.load_bed(os.path.join(tool, "empty-bed-map"), os.path.join(tool, "empty-bed"))
            if bed is not None:
                self._set_bed(bed)
        L = self.learning()
        index = os.path.join(src, "examples.jsonl")
        if not os.path.exists(index):
            return 0
        have = {e.id for e in L.store.examples}
        added = 0
        with L.lock, open(index) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec["id"] in have:
                    continue
                rec.setdefault("shape", LidMatch.shape_of_name(rec.get("size")))
                rec.setdefault("source", "teaching tool")
                for sub, ext in (("photos", ".jpg"), ("crops", ".png"), ("masks", ".png")):
                    p = os.path.join(src, sub, rec["id"] + ext)
                    if os.path.exists(p):
                        shutil.copy2(p, os.path.join(L.store.folder, sub, rec["id"] + ext))
                with open(L.store.index, "a") as out:
                    out.write(json.dumps(rec) + "\n")
                L.store.examples.append(LidLearn.Example(rec))
                added += 1
            L.store.load_features(L.network)
            L.pair_cache.clear()
            L.trainer = LidLearn.Trainer(L.outlines, L.pair_cache)
        return added

    # ============================================================ settings
    def show_settings_dialog(self):
        f = self.frame
        d = Toplevel(f)
        d.title(_("Lid Camera"))
        d.transient(f)
        d.withdraw()
        body = Frame(d, padx=15, pady=15)
        body.pack(fill=BOTH, expand=YES)

        on = BooleanVar(value=enabled())
        auto = BooleanVar(value=auto_after_scan())
        scope = StringVar(value=candidate_scope())
        mm = StringVar(value="%g" % marker_mm())
        scale = StringVar(value="%.4f" % size_scale())
        msg = StringVar()

        Checkbutton(body, text=_("Use the lid camera"), variable=on).grid(
            row=0, column=0, columnspan=2, sticky=W)
        Checkbutton(body, text=_("Identify the lid as soon as a tote is scanned"),
                    variable=auto).grid(row=1, column=0, columnspan=2, sticky=W)
        Label(body, text=_("Lids to choose among:")).grid(row=2, column=0, sticky="e", padx=(0, 5))
        sc = Frame(body)
        sc.grid(row=2, column=1, sticky=W)
        Radiobutton(sc, text=_("the tote on show"), variable=scope, value="tote").pack(side=TOP, anchor=W)
        Radiobutton(sc, text=_("every order scanned here with lids waiting (%d h)") % ORDER_KEEP_H,
                    variable=scope, value="all").pack(side=TOP, anchor=W)
        Label(body, text=_("Marker size (mm):")).grid(row=3, column=0, sticky="e", padx=(0, 5), pady=4)
        Entry(body, textvariable=mm, width=8).grid(row=3, column=1, sticky=W)
        Label(body, text=_("The black square only, not its white border, as printed."),
              font=("", 8, "italic"), fg="gray").grid(row=4, column=1, sticky=W)
        Label(body, text=_("Size correction:")).grid(row=5, column=0, sticky="e", padx=(0, 5), pady=4)
        Entry(body, textvariable=scale, width=8).grid(row=5, column=1, sticky=W)
        Label(body, text=_("Set by 'Calibrate Size' below; 1 means none."),
              font=("", 8, "italic"), fg="gray").grid(row=6, column=1, sticky=W)

        def busy_msg():
            r = self.why_not()
            if r:
                msg.set(r)
            return r

        # -------------------------------------------------- the bed
        bedf = LabelFrame(body, text=_("The bed"), padx=6, pady=6)
        bedf.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        bed_state = StringVar()
        corner_state = StringVar()

        def refresh_states():
            b = self.bed()
            bed_state.set(_("Empty-bed map: %d%% of its area known") % round(100 * (b.count > 0).mean())
                          if b is not None else _("Empty-bed map: none (lids found by their edges)"))
            corner_state.set(_("Fixture corner: set") if load_corner() is not None
                             else _("Fixture corner: not set"))

        refresh_states()
        Label(bedf, textvariable=bed_state, anchor=W).pack(side=TOP, fill=X)
        Label(bedf, textvariable=corner_state, anchor=W).pack(side=TOP, fill=X)

        def add_empty_bed():
            if busy_msg():
                return
            if not messagebox.askokcancel(
                    _("Empty-Bed Photo"),
                    _("Take every lid off the bed, leave the markers where they are, and press "
                      "OK.\n\nThe photo is added to the empty-bed map: what it shows replaces "
                      "what the map had there."), parent=d):
                return
            msg.set(_("Taking the empty-bed photo..."))
            mm_v = marker_mm()

            def work():
                shot = self._photo()
                return LidVision.identify(shot.jpeg, LidMatch.detection_candidates(),
                                          lambda k: None, mm_v)

            def done(res):
                if res.plane is None:
                    msg.set(_("No markers in view - nothing was saved."))
                    return
                if res.lid is not None and not messagebox.askyesno(
                        _("Empty-Bed Photo"),
                        _("There seems to be a lid on the bed (%s). An empty-bed photo must "
                          "show no lid. Save it anyway?") % LidVision.describe_size(res.lid),
                        parent=d):
                    msg.set(_("Not saved."))
                    return
                old = self.bed()
                if old is not None and old.ref == res.plane.ref and old.marker_mm == mm_v:
                    new = old.copy()
                    new.add(res.rectified, res.valid, res.region, res.ppm, replace=True)
                else:
                    new = LidVision.BedMap.from_result(res, mm_v)
                self._set_bed(new)
                refresh_states()
                msg.set(_("Added to the empty-bed map."))

            self._start(work, done, lambda e: msg.set(_("Camera: %s") % e))

        def drop_bed():
            if messagebox.askyesno(_("Empty-Bed Map"), _("Clear the empty-bed map? Lids are then "
                                                          "found by their edges until a new one is "
                                                          "made."), parent=d):
                self._set_bed(None)
                refresh_states()
                msg.set(_("Empty-bed map cleared."))

        def set_corner():
            if busy_msg():
                return
            if not messagebox.askokcancel(
                    _("Fixture Corner"),
                    _("Push a light-coloured lid into the fixture's corner the way every lid "
                      "goes, fully in view, and press OK."), parent=d):
                return
            msg.set(_("Finding the lid..."))
            mm_v, bed = marker_mm(), self.bed()

            def work():
                shot = self._photo()
                return LidVision.identify(shot.jpeg, LidMatch.detection_candidates(),
                                          lambda k: None, mm_v, 1.0, bed)

            def done(res):
                corner = LidVision.corner_from_result(res)
                if corner is None or res.lid.length_is_min:
                    msg.set(_("No lid found fully in view - nothing was saved."))
                    return
                save_corner(corner)
                refresh_states()
                msg.set(_("Fixture corner saved from a %s lid.") % LidVision.describe_size(res.lid))

            self._start(work, done, lambda e: msg.set(_("Camera: %s") % e))

        row = Frame(bedf)
        row.pack(side=TOP, fill=X, pady=(4, 0))
        Button(row, text=_("Add Empty-Bed Photo"), command=add_empty_bed).pack(side=LEFT, padx=2)
        Button(row, text=_("Clear Map"), command=drop_bed).pack(side=LEFT, padx=2)
        Button(row, text=_("Set Fixture Corner"), command=set_corner).pack(side=LEFT, padx=(12, 2))

        # ------------------------------------------------- the camera
        camf = LabelFrame(body, text=_("The camera"), padx=6, pady=6)
        camf.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        rec = BltCamera.load_exposure(Utils.iniUser + BltCamera.EXPOSURE_FILE_SUFFIX)
        Label(camf, anchor=W, text=(_("Exposure: fixed (%s, calibrated %s)") % (
            rec["aec"], rec.get("calibrated", "?")) if rec else _("Exposure: automatic"))).pack(
            side=TOP, fill=X)

        def calibrate_size():
            if busy_msg():
                return
            i, row_ = f._selected()
            lid = f._lid_for(i) if row_ is not None else None
            want = PillcaseOrder.lid_outline(self.pool._lid_defaults().get(lid)) if lid else None
            if not want:
                msg.set(_("Select a lid in the tote list, with its size configured, and "
                          "place that lid in the fixture first."))
                return
            msg.set(_("Measuring %s...") % lid)
            mm_v, bed, corner = marker_mm(), self.bed(), load_corner()

            def work():
                shot = self._photo()
                return LidVision.identify(shot.jpeg, LidMatch.detection_candidates(),
                                          lambda k: None, mm_v, 1.0, bed, corner)

            def done(res):
                if res.lid is None:
                    msg.set(_("No lid found: %s") % " ".join(res.warnings))
                    return
                if res.lid.length_is_min:
                    msg.set(_("The lid runs off the edge of the camera's view, so its length "
                              "cannot be measured. Calibrate with a lid that is fully in view."))
                    return
                w_short, w_long = sorted(want)
                m_short, m_long = res.lid.size_mm
                # Weighted to the long side: the same pixel error is a smaller
                # fraction of it.
                k = (w_short + w_long) / (m_short + m_long)
                scale.set("%.4f" % k)
                msg.set(_("Measured %.1f x %.1f mm for a %.1f x %.1f mm lid. "
                          "Press Save to keep the correction.") % (m_short, m_long, w_short, w_long))

            self._start(work, done, lambda e: msg.set(_("Camera: %s") % e))

        row = Frame(camf)
        row.pack(side=TOP, fill=X, pady=(4, 0))
        Button(row, text=_("Calibrate Exposure..."),
               command=lambda: self.calibrate_exposure(d, msg)).pack(side=LEFT, padx=2)
        Button(row, text=_("Calibrate Size with Selected Lid"), command=calibrate_size).pack(
            side=LEFT, padx=2)

        # ----------------------------------------------- what it knows
        learnf = LabelFrame(body, text=_("What the camera has learned"), padx=6, pady=6)
        learnf.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        learn_state = StringVar(value=_("Named lids: loading..."))
        Label(learnf, textvariable=learn_state, anchor=W, justify=LEFT).pack(side=TOP, fill=X)

        def describe_learning():
            L = self._learning
            if L is None:
                return
            ex = L.store.examples
            m, hit = LidMatch.best_colour_method(ex, L.network)
            learn_state.set(_("Named lids: %d, %d colours. Colour judged by: %s%s.\n"
                              "Image network: %s.") % (
                len(ex), len({e.colour for e in ex}), m,
                "" if hit is None else _(" (right %d%% lately)") % round(100 * hit),
                _("in use") if L.network is not None else _("not fetched")))

        def load_learning():
            self._start(self.learning, lambda L: describe_learning(),
                        lambda e: learn_state.set(_("Named lids: could not load (%s)") % e))

        if self._job is None:
            load_learning()

        def do_import():
            if busy_msg():
                return
            msg.set(_("Importing the teaching tool's lids..."))
            self._start(self.import_teaching, lambda n: (msg.set(_("Imported %d named lids.") % n),
                                                         describe_learning()),
                        lambda e: msg.set(_("Import failed: %s") % e))

        def fetch_network():
            if busy_msg():
                return
            if not messagebox.askokcancel(
                    _("Image Network"),
                    _("Download the image network (MobileNetV2, 14 MB, checked against its "
                      "known hash)? Colour and pattern matching use it from then on."), parent=d):
                return
            msg.set(_("Downloading..."))

            def work():
                LidLearn.fetch_model(_path("models"))
                with self._learning_lock:
                    self._learning = None           # rebuilt with the network
                return self.learning()

            self._start(work, lambda L: (msg.set(_("Image network ready.")), describe_learning()),
                        lambda e: msg.set(_("Download failed: %s") % e))

        def forget_colours():
            if messagebox.askyesno(_("Forget Colours"),
                                   _("Forget the colours averaged from confirmed lids? The named "
                                     "lids are kept."), parent=d):
                self.colours.forget()
                msg.set(_("Averaged colours cleared."))

        row = Frame(learnf)
        row.pack(side=TOP, fill=X, pady=(4, 0))
        Button(row, text=_("Import from Teaching Tool"),
               command=lambda: (do_import(), d.after(3000, refresh_states))).pack(side=LEFT, padx=2)
        Button(row, text=_("Get Image Network"), command=fetch_network).pack(side=LEFT, padx=2)
        Button(row, text=_("Forget Averaged Colours"), command=forget_colours).pack(side=LEFT, padx=2)

        Label(body, textvariable=msg, fg="DarkBlue", wraplength=460, justify=LEFT).grid(
            row=10, column=0, columnspan=2, sticky=W, pady=(6, 0))

        def save():
            try:
                mm_v = float(mm.get())
                scale_v = float(scale.get())
                if mm_v <= 0 or not 0.5 < scale_v < 2.0:
                    raise ValueError
            except ValueError:
                msg.set(_("Marker size must be above 0, and the correction between 0.5 and 2."))
                return
            Utils.addSection(_SECTION)
            Utils.setBool(_SECTION, "enabled", on.get())
            Utils.setBool(_SECTION, "autoAfterScan", auto.get())
            Utils.setStr(_SECTION, "candidates", scope.get())
            Utils.setStr(_SECTION, "markerMM", "%g" % mm_v)
            Utils.setStr(_SECTION, "sizeScale", "%.4f" % scale_v)
            d.destroy()
            f.update_camera_button()

        bf = Frame(body)
        bf.grid(row=11, column=0, columnspan=2, sticky="e", pady=(10, 0))
        Button(bf, text=_("Save"), width=12, command=save).pack(side=LEFT, padx=5)
        Button(bf, text=_("Cancel"), width=12, command=d.destroy).pack(side=LEFT)

        f._gen()._fit_and_center(d)
        d.deiconify()
        d.grab_set()
        f.wait_window(d)

    # ================================================== exposure calibration
    def calibrate_exposure(self, parent, msg):
        """The two-lid exposure calibration (LidExposure), in a small window."""
        import LidExposure

        if self.why_not():
            msg.set(self.why_not())
            return
        if getattr(self.app, "blt_camera_version", 0) and \
                self.app.blt_camera_version < BltCamera.EXPOSURE_VERSION:
            msg.set(_("The camera firmware cannot fix its exposure (flash firmware/bltouch_camera)."))
            return
        d = Toplevel(parent)
        d.title(_("Calibrate Exposure"))
        d.transient(parent)
        prompt = StringVar()
        Label(d, textvariable=prompt, font=("", 11, "bold"), justify=LEFT, anchor=W,
              wraplength=520).pack(side=TOP, fill=X, padx=12, pady=(12, 6))
        log = Text(d, width=70, height=14, font=("TkFixedFont", 9))
        log.pack(side=TOP, fill=BOTH, expand=YES, padx=12)
        bar = Frame(d)
        bar.pack(side=BOTTOM, fill=X, padx=12, pady=10)
        go = Button(bar, width=12)
        go.pack(side=RIGHT)
        st = {"stage": "bright", "settings": None, "area": None, "bright": None,
              "dark": None, "verdict": None, "saved": False}
        lines = []

        def write(text):
            lines.append(text)

        def flush():
            while lines:
                log.insert(END, lines.pop(0) + "\n")
                log.see(END)
            if st["stage"] in ("busy-bright", "busy-dark"):
                d.after(150, flush)

        def take(aec, agc, wb):
            self.app.blt_camera_exposure(aec, agc, wb)
            return self.app.blt_camera_capture().jpeg

        def stage(name):
            st["stage"] = name
            texts = {
                "bright": (_("Place the BRIGHTEST lid (Aluminum) where lids go, fully in view, "
                             "then press Measure. About 15 seconds."), _("Measure")),
                "dark": (_("Now swap in the DARKEST lid (Matte Black) at the same spot, the same "
                           "way round, then press Measure."), _("Measure")),
                "done": (_("Save these settings? Every photo is then taken with them."), _("Save")),
            }
            if name.startswith("busy"):
                prompt.set(_("Measuring... keep the lid still."))
                go.config(state="disabled")
                flush()
                return
            prompt.set(texts[name][0])
            go.config(text=texts[name][1], state="normal")

        def failed(e):
            write(_("Stopped: %s") % e)
            flush()
            stage("bright" if st["stage"] == "busy-bright" else "dark")

        def step():
            if st["stage"] == "bright":
                stage("busy-bright")
                mm_v = marker_mm()

                def done(v):
                    st["settings"], st["area"], st["bright"] = v
                    write(_("Bright lid: exposure %d, %s white balance; its brightest %.0f of 255.")
                          % (st["settings"]["aec"], BltCamera.WB_PRESETS[st["settings"]["wb"]],
                             st["bright"]["lid_hi"]))
                    stage("dark")
                    flush()

                self._start(lambda: LidExposure.calibrate_bright(
                    take, LidMatch.detection_candidates(), mm_v, None, log=write), done, failed)
            elif st["stage"] == "dark":
                stage("busy-dark")

                def done(v):
                    st["dark"], st["verdict"], ok = v
                    write(st["verdict"])
                    stage("done" if ok else "dark")
                    flush()

                self._start(lambda: LidExposure.check_dark(take, st["settings"], st["area"],
                                                           st["bright"], log=write), done, failed)
            elif st["stage"] == "done":
                rec = LidExposure.record(st["settings"], st["bright"], st["dark"], st["verdict"])
                BltCamera.save_exposure(Utils.iniUser + BltCamera.EXPOSURE_FILE_SUFFIX, rec)
                st["saved"] = True
                msg.set(_("Exposure saved. Add a new empty-bed photo: the old one was taken with "
                          "the old exposure."))
                close()

        def close():
            # Not saved: back to what was in use before.
            if not st["saved"] and self._job is None:
                prev = BltCamera.load_exposure(Utils.iniUser + BltCamera.EXPOSURE_FILE_SUFFIX)
                try:
                    if prev:
                        self.app.blt_camera_exposure(prev["aec"], prev["agc"], prev["wb"])
                    else:
                        self.app.blt_camera_exposure(None)
                except Exception as e:
                    print("[lidcam] exposure not restored:", e)
            else:
                rec = BltCamera.load_exposure(Utils.iniUser + BltCamera.EXPOSURE_FILE_SUFFIX)
                if rec:
                    try:
                        self.app.blt_camera_exposure(rec["aec"], rec["agc"], rec["wb"])
                    except Exception as e:
                        print("[lidcam] exposure not applied:", e)
            d.destroy()

        go.config(command=step)
        Button(bar, text=_("Cancel"), width=10, command=close).pack(side=RIGHT, padx=6)
        d.protocol("WM_DELETE_WINDOW", close)
        stage("bright")
