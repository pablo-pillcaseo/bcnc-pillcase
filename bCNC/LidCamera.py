"""Lid camera for Tote Scanning: see the lid placed in the fixture, pick it in
the list, and show its engraving on it.

While lids are waiting to be engraved and the machine is idle, the camera on
the BLTouch board looks at the bed every couple of seconds, and its latest
photo takes the place of the G-code view: the engraver sees the bed. A lid
that appears and holds still for two looks is identified: LidVision finds and
measures it, and LidMatch gives the odds of each lid waiting in the open totes
(the candidates, below). The likeliest is selected in Tote Scanning's list and
its preview made at once: the engraving over a faint photo of this very lid,
with a panel beside it showing the lid's photo above one of the same lid from
the library. One press of Confirm then engraves it. After the engraving the
camera waits for the lid to be taken off before it looks for the next.

The camera learns as it is used (LidLearn), never in a separate tool:
  * A lid it was sure of - a colour it has named lids of, and the odds high -
    is kept as a named lid when the engraver confirms it.
  * A lid the engraver picked (the camera unsure, wrong, or with nothing to
    compare with) gets Retake and Save to Library in the panel: the engraver
    keeps the photo once it is a good one, labelled with the lid picked.
Either way the bare fixture round the lid updates the empty-bed map.

The candidates are the lids still to engrave in every open tote (Tote
Scanning keeps several open at once), so a lid from any of them may be placed;
a lid engraved on another station drops out through the shared progress.

Nothing here can delay probing:
  * No photo is taken while the machine runs, Align & Run is probing, Auto
    Setup is working, or a preview waits to be started. The check is made
    before each photo, and Confirm & Engrave waits for a photo in flight.
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

import atexit
import json
import os
import threading
import time
import tkinter as tk
from tkinter import (
    BOTH, BOTTOM, END, LEFT, RIGHT, TOP, W, X, YES,
    BooleanVar, Button, Checkbutton, Entry, Frame, Label, LabelFrame,
    StringVar, Text, Toplevel, messagebox, ttk,
)

import numpy as np

import BltCamera
import LidLearn
import LidMatch
import LidVision
import PillcaseOrder
import Utils
from Utils import _

_SECTION = "LidCamera"
POLL_MS = 100

# Between looks at the bed: waiting for a lid, or for one to be taken off...
WATCH_MS = 1500
# ...and while an identified lid sits there (to notice it being swapped).
SEEN_MS = 3000
# After the camera fails (unplugged, say), before it tries again.
RETRY_MS = 15000
# Two looks agree on a lid when they put it this close, at this size...
SAME_MM = 4.0
# ...and this close in colour (in LidVision.COLOUR_SIGMA).
SAME_COLOUR = 4.0
# A photo stays the lid on the bed (and teaches the camera) this long.
TEACH_BY_HAND_S = 300
# The camera picks a lid by itself (and learns from it when confirmed) only
# this sure, and only of a colour it has named lids of.
SURE_P = 0.8
# Where the fixture corner falls in the preview, for the photo under it.
BACKDROP_CORNERS = ("top left", "top right", "bottom left", "bottom right")
BACKDROP_PPM = 8.0


def _path(suffix):
    return Utils.iniUser + "-lidcam-" + suffix


def enabled():
    return Utils.getBool(_SECTION, "enabled", True)


def watching():
    """Look for a lid being placed, or only when Look Again is pressed."""
    return Utils.getBool(_SECTION, "watch", True)


def backdrop_on():
    return Utils.getBool(_SECTION, "backdrop", True)


def backdrop_corner():
    v = Utils.getStr(_SECTION, "backdropCorner", "top left")
    return v if v in BACKDROP_CORNERS else "top left"


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
    p = os.path.join(_path("models"), LidLearn.MODEL_FILE)
    return p if os.path.exists(p) else None


# ------------------------------------------------------------- the lid seen
def lid_place(res):
    """Where a look put the lid, to compare looks: centre (marker mm), size
    and colour. None without a lid - or with one outside the fixture's
    corner, where lids are never placed: the edge search finding something
    in the clutter of an empty bed."""
    if res.lid is None or res.region is None or res.in_corner is False:
        return None
    cx, cy = res.lid.centre_mm
    return {"centre": (cx + res.region[0], cy + res.region[1]), "size": res.lid.size_mm,
            "colour": res.colour}


def same_lid(a, b):
    """Two looks saw the same lid, in the same place."""
    if a is None or b is None:
        return False
    if np.hypot(a["centre"][0] - b["centre"][0], a["centre"][1] - b["centre"][1]) > SAME_MM:
        return False
    if max(abs(x - y) for x, y in zip(a["size"], b["size"])) > SAME_MM:
        return False
    if a["colour"] is not None and b["colour"] is not None:
        if LidVision.colour_distance(a["colour"], b["colour"]) > SAME_COLOUR * LidVision.COLOUR_SIGMA:
            return False
    return True


def backdrop_image(flat, ppm, length_is_min, frame_mm, corner_at, out_ppm=BACKDROP_PPM):
    """The flat lid (LidVision.flat_lid, at `ppm`) placed on its engraving
    frame (`frame_mm` = (width, length), the preview's X and Y): to scale,
    from the fixture corner, which is at the frame's `corner_at`. A lid
    larger than the frame is cut to it; where it is smaller the frame stays
    clear. -> RGBA array, `out_ppm` pixels per mm."""
    import cv2 as cv
    fw, fl = (max(1, int(round(v * out_ppm))) for v in frame_mm)
    k = out_ppm / ppm
    lid = cv.resize(flat, (max(1, int(round(flat.shape[1] * k))), max(1, int(round(flat.shape[0] * k)))),
                    interpolation=cv.INTER_CUBIC)
    lid = cv.cvtColor(lid, cv.COLOR_BGR2RGB)
    if length_is_min and lid.shape[0] < fl:
        # It ran off the camera's view: the rest of it is the same lid.
        fill = np.median(lid.reshape(-1, 3), axis=0).astype(np.uint8)
        lid = np.vstack([lid, np.tile(fill, (fl - lid.shape[0], lid.shape[1], 1))])
    h, w = min(lid.shape[0], fl), min(lid.shape[1], fw)
    part = lid[:h, :w]
    x0 = y0 = 0
    if "right" in corner_at:
        part, x0 = part[:, ::-1], fw - w
    if "bottom" in corner_at:
        part, y0 = part[::-1], fl - h
    out = np.zeros((fl, fw, 4), np.uint8)
    out[y0:y0 + h, x0:x0 + w, :3] = part
    out[y0:y0 + h, x0:x0 + w, 3] = 255
    return out


# ------------------------------------------------------------ the pool
def candidates(frame):
    """The lids waiting to be engraved, in every open tote: the camera chooses
    among these. The tote being worked on leads, so among lids that look
    alike its rows come first; then the newest."""
    gen = frame._gen()
    if not hasattr(gen, "_lid_defaults"):
        gen._lid_defaults = gen._load_lid_defaults()
    defaults = gen._lid_defaults
    focus = frame.focus_tote()
    out = []
    for tote, i in sorted(frame.waiting(), key=lambda ti: ti[0] is not focus):
        row = tote.rows[i]
        lid = frame._lid_for(tote, i)
        out.append({
            "key": "%s|%s" % (tote.key, tote.row_keys[i]),
            # The lid as it is, not its engraving frame (see lid_outline).
            "size": PillcaseOrder.lid_outline(defaults.get(lid)) if lid else None,
            "colour_key": (row.get("colour_code") or "").upper(),
            "lid": lid, "row": row, "row_index": i, "row_key": tote.row_keys[i],
            "tote_key": tote.key, "scan": tote.scan, "mode": tote.mode,
        })
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


# ========================================================= the camera view
def _tk_image(bgr, width, height):
    """`bgr` scaled to fit width x height, as a Tk image (None if it cannot)."""
    try:
        import cv2 as cv
        from PIL import Image, ImageTk
    except ImportError:
        return None
    ih, iw = bgr.shape[:2]
    k = min(width / float(iw), height / float(ih))
    if k <= 0:
        return None
    small = cv.resize(bgr, (max(1, int(iw * k)), max(1, int(ih * k))), interpolation=cv.INTER_AREA)
    return ImageTk.PhotoImage(Image.fromarray(cv.cvtColor(small, cv.COLOR_BGR2RGB)))


class CameraView:
    """The lid camera in bCNC's G-code view, laid over that canvas.

    live(): the camera's latest photo over the whole view, while a lid is
    awaited or could not be matched. A click on it shows the G-code view.
    panel(): down the view's right side, beside the lid's preview: this lid's
    photo above the same lid's from the library, and the engraver's buttons.
    """

    PANEL_WIDTH = 0.36      # of the view
    BG = "#1e1e1e"

    def __init__(self, app):
        self.app = app
        self.cv = None
        self.bgr = None
        self.caption = ("", "white")
        self._photo = None
        self.box = None             # the panel
        self._content = None        # what the panel shows (panel()'s arguments)
        self._shown = 0             # counts panel() calls: each is new content
        self._filled = None         # (content count, width) the panel was last filled for
        self._photos = []

    def _target(self):
        return getattr(getattr(self.app, "canvasFrame", None), "canvas", None)

    def _raise(self, widget, target):
        # Canvas.lift raises canvas items: the window itself is raised here.
        widget.tk.call("raise", widget._w, target._w)

    # --------------------------------------------------------------- live
    def live(self, bgr, caption, colour="white"):
        target = self._target()
        if target is None or bgr is None:
            return
        if self.cv is None:
            self.cv = tk.Canvas(target.master, highlightthickness=0, background=self.BG,
                                cursor="hand2")
            self.cv.bind("<Configure>", lambda e: self._render())
            self.cv.bind("<Button-1>", lambda e: self.hide())
        self.hide_panel()
        self.bgr, self.caption = bgr, (caption, colour)
        self.cv.place(in_=target, x=0, y=0, relwidth=1, relheight=1)
        self._raise(self.cv, target)
        self._render()

    def hide(self):
        if self.cv is not None:
            self.cv.place_forget()

    def shown(self):
        return self.cv is not None and self.cv.winfo_manager() == "place"

    def _render(self):
        cv_ = self.cv
        if cv_ is None or self.bgr is None:
            return
        w, h = cv_.winfo_width(), cv_.winfo_height()
        if w < 20 or h < 20:
            return
        cv_.delete("all")
        text, colour = self.caption
        top = cv_.create_text(10, 6, text=text, anchor="nw", fill=colour,
                              font=("", 13, "bold"), width=w - 20)
        foot = cv_.create_text(w - 10, h - 4, anchor="se", fill="#9a9a9a", font=("", 9),
                               text=_("Lid camera  ·  click for the G-code view"))
        y0 = cv_.bbox(top)[3] + 6
        y1 = cv_.bbox(foot)[1] - 4
        self._photo = _tk_image(self.bgr, w, y1 - y0)
        if self._photo is not None:
            cv_.create_image(w // 2, (y0 + y1) // 2, image=self._photo)

    # -------------------------------------------------------------- panel
    def panel(self, caption, colour, this_lid, library, library_caption, note, actions=()):
        """The panel beside the preview. `this_lid` / `library`: BGR crops
        (long side across) or None; `actions`: [(text, command)] buttons."""
        target = self._target()
        if target is None:
            return
        if self.box is None:
            self.box = tk.Frame(target.master, background=self.BG, padx=10, pady=8)
            self.box.bind("<Configure>", lambda e: self._fill_panel())
        self.hide()
        self._content = (caption, colour, this_lid, library, library_caption, note, tuple(actions))
        self._shown += 1
        self.box.place(in_=target, relx=1.0, x=0, y=0, anchor="ne",
                       relwidth=self.PANEL_WIDTH, relheight=1)
        self._raise(self.box, target)
        self._fill_panel()

    def hide_panel(self):
        if self.box is not None:
            self.box.place_forget()
        self._content = None

    def hide_all(self):
        self.hide()
        self.hide_panel()

    def _fill_panel(self):
        box, content = self.box, self._content
        if box is None or content is None:
            return
        caption, colour, this_lid, library, library_caption, note, actions = content
        w = max(100, box.winfo_width() - 20)
        # Refilling resizes the children, which reports the box's size again:
        # refilled only for new content or a new width.
        if self._filled == (self._shown, w):
            return
        self._filled = (self._shown, w)
        for child in box.winfo_children():
            child.destroy()
        self._photos = []

        def text(t, fg="#d0d0d0", font=("", 10), pady=(0, 4)):
            tk.Label(box, text=t, fg=fg, bg=self.BG, font=font, justify="left", anchor="w",
                     wraplength=w).pack(side="top", fill="x", pady=pady)

        def picture(bgr, missing):
            img = _tk_image(bgr, w, w * 0.6) if bgr is not None else None
            if img is None:
                text(missing, fg="#9a9a9a", font=("", 10, "italic"), pady=(0, 12))
                return
            self._photos.append(img)
            tk.Label(box, image=img, bg=self.BG).pack(side="top", anchor="w", pady=(0, 12))

        text(caption, fg=colour, font=("", 13, "bold"), pady=(0, 10))
        text(_("This lid, on the bed"), fg="#9a9a9a", font=("", 9))
        picture(this_lid, _("No photo of this lid."))
        text(library_caption, fg="#9a9a9a", font=("", 9))
        picture(library, _("No photo of this lid in the library yet."))
        if note:
            text(note, fg="#ffcc66")
        if actions:
            row = tk.Frame(box, bg=self.BG)
            row.pack(side="top", fill="x", pady=(6, 0))
            for label, command in actions:
                tk.Button(row, text=label, command=command, font=("", 11, "bold"),
                          padx=10, pady=4).pack(side="left", padx=(0, 8))


# ========================================================== the controller
class Controller:
    """The camera side of an EngravingFrame (`frame`).

    It watches in one of two states: "empty", waiting for a lid to be placed,
    and "on", a lid on the bed - identified, or just engraved - waiting for it
    to be taken off or swapped.
    """

    def __init__(self, frame):
        self.frame = frame
        self.app = frame.app
        self.colours = LidVision.ColourMemory(_path("colours.json"))
        self.view = CameraView(self.app)
        self._thumb_colours = {}
        self._job = None            # worker in flight: {"thread", "result", "error", "done"}
        self._learning = None       # Learning, once loaded
        self._learning_lock = threading.Lock()
        self._bed = None            # the empty-bed map in use (swapped whole when changed)
        self._bed_loaded = False
        self._last = None           # the identified lid's outcome, until taken off
        self.state = "empty"
        self._prev = None           # the previous look's lid_place
        self._on = None             # lid_place of the lid on the bed ("on")
        self._empty_looks = 0
        self._retry_at = 0.0
        self._idle_waiters = []
        self._keep_pick = False     # a retake: the engraver's pick stays selected
        frame.after(WATCH_MS, self._tick)
        self._fetch_network()
        # A look still running in OpenCV when Python exits aborts the process.
        atexit.register(self._finish_look)

    def _fetch_network(self):
        """The image network, downloaded once in the background (14 MB): until
        then, and without a connection, lids are matched without it."""
        if not enabled() or model_path() is not None:
            return

        def run():
            try:
                LidLearn.fetch_model(_path("models"))
            except Exception as e:
                print("[lidcam] image network not fetched:", e)
                return
            with self._learning_lock:
                self._learning = None       # rebuilt with the network on next use

        threading.Thread(target=run, daemon=True).start()

    def _finish_look(self, limit_s=10.0):
        job, t0 = self._job, time.time()
        while job is not None and not job["done"] and time.time() - t0 < limit_s:
            time.sleep(0.05)

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

    def when_idle(self, fn):
        """Call `fn` once no photo is being taken (now, if none is)."""
        if self._job is None:
            fn()
        else:
            self._idle_waiters.append(fn)

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
            try:
                if job["error"] is not None:
                    on_error(job["error"])
                else:
                    on_done(job["result"])
            finally:
                waiters, self._idle_waiters = self._idle_waiters, []
                for fn in waiters:
                    fn()

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

    # ============================================================ watching
    def _tick(self):
        delay = WATCH_MS
        try:
            delay = self._watch()
        except Exception as e:
            print("[lidcam] watch error:", repr(e))
        self.frame.after(delay, self._tick)

    def _watch(self):
        """One step of watching the bed; returns when to step again (ms)."""
        f = self.frame
        if not (enabled() and watching()) or self._job is not None:
            return WATCH_MS
        if self.app.blt_serial is None or self.app.blt_has_camera is False \
                or not LidVision.available():
            return WATCH_MS     # no camera here: Tote Scanning works as before
        if not f.session.logged_in or f.armed() or time.time() < self._retry_at:
            return WATCH_MS
        if self.state == "empty" and not f.waiting():
            return WATCH_MS
        if self.why_not() is not None:
            return WATCH_MS
        self.look()
        return SEEN_MS if self.state == "on" and self._last is not None else WATCH_MS

    def totes_changed(self):
        """Totes were opened or closed: odds for lids no longer waiting go."""
        if self._last is not None and self.state == "on":
            self._present(self._last, rematch=True)
        elif not self.frame.waiting():
            self.frame.set_camera_prompt(None)
            self.view.hide_all()

    def engraved(self):
        """The lid on the bed was just engraved: wait for it to be taken off."""
        self.state = "on"
        self._last = None
        self._empty_looks = 0
        self.frame.show_odds({}, None)
        self.view.hide_panel()
        if enabled():
            self.frame.set_camera_prompt(_("Take the engraved lid off, and place the next."))

    # ============================================================== a look
    def look(self, force=False):
        """Take a photo and act on it. `force`: identify whatever is there,
        as asked for by Look Again."""
        f = self.frame
        reason = self.why_not()
        if reason:
            if force:
                f._warn(reason)
            return
        if force and not f.waiting():
            f._warn(_("Scan a tote first - there are no lids waiting to match."))
            return
        bed, corner = self.bed(), load_corner()
        mm, scale = marker_mm(), size_scale()
        if force:
            f._status(_("📷 Looking at the bed..."))

        def work():
            shot = self._photo()
            res = LidVision.identify(shot.jpeg, LidMatch.detection_candidates(), lambda k: None,
                                     mm, scale, bed, corner)
            return shot, res

        def failed(e):
            self._retry_at = time.time() + RETRY_MS / 1000.0
            msg = _("Lid camera: %s") % e
            f._status(msg, error=force)
            if force:
                f._warn(msg)

        self._start(work, lambda out: self._looked(out[0], out[1], force, bed), failed)

    def _looked(self, shot, res, force, bed):
        """What a look saw, on the Tk thread: a lid placed, taken off, swapped."""
        place = lid_place(res)
        prev, self._prev = self._prev, place
        if not force and self.state == "empty" and self.frame.waiting():
            # Live: the bed as it is now, until a lid is identified
            self.view.live(res.annotated, _("📷 Place a lid from the order in the fixture.")
                           if place is None else _("📷 Hold on - looking at the lid..."))
            if place is None:
                self.frame.set_camera_prompt(_("📷 Place a lid in the fixture."))
        if force:
            self._identify(shot, res, bed, place)
            return
        if place is None:
            self._empty_looks += 1
            if self.state == "on" and self._empty_looks >= 2:
                self._taken_off()
            return
        self._empty_looks = 0
        # A lid, still for two looks, that is not the one already on the bed
        if same_lid(place, prev) and not (self.state == "on" and same_lid(place, self._on)):
            self._identify(shot, res, bed, place)

    def _taken_off(self):
        self.state, self._on, self._last = "empty", None, None
        self.frame.show_odds({}, None)
        self.view.hide_panel()
        self.frame.set_camera_prompt(_("📷 Place a lid in the fixture.") if self.frame.waiting()
                                     else None)

    def _identify(self, shot, res, bed, place):
        """The lid on the bed is new: give the odds of each waiting lid."""
        cands = candidates(self.frame)
        refs = {}
        for c in cands:
            if c["colour_key"] not in refs:
                refs[c["colour_key"]] = self._reference_colour(c["colour_key"],
                                                               c["row"].get("colour_name"))
        self.state, self._on = ("on", place) if place is not None else ("empty", None)
        self._start(lambda: self._analyse(shot, res, cands, refs, bed),
                    self._present,
                    lambda e: self.frame._status(_("Lid camera: %s") % e, error=True))

    def _analyse(self, shot, res, cands, refs, bed):
        """Worker thread: describe the lid found, and give the odds."""
        L = self.learning()
        crop = mask = feats = None
        if res.lid is not None:
            crop, mask = LidLearn.lid_crop(res.rectified, res.lid, res.ppm)
            feats = LidLearn.features(crop, mask, L.network)
        out = {"shot": shot, "res": res, "crop": crop, "mask": mask, "feats": feats,
               "candidates": cands, "refs": refs, "time": time.time(), "taught": False,
               "empty_bed": bed is not None}
        self._match(out)
        return out

    def _match(self, out):
        """The odds of out's candidates (any thread)."""
        L = self.learning()
        with L.lock:
            examples = list(L.store.examples)
            usable = [e for e in examples if e.features is not None]
            memory = LidLearn.Memory(usable, L.outlines, L.pair_cache)
            learned = L.trainer.get(usable)
            method, hit = LidMatch.best_colour_method(examples, L.network)
            out["match"] = LidMatch.match(out["res"], out["candidates"], out["feats"], memory,
                                          learned, examples, out["refs"].get, method, hit)
            out["guesses"] = LidLearn.guesses(memory, out["feats"], out["res"].lid, learned)

    def _present(self, out, rematch=False):
        """Act on what the camera made of the lid: the odds in the list, its
        best guess selected, and the frame told to make that lid's preview
        (camera_ready), which brings up the panel (show_panel)."""
        f = self.frame
        if rematch:
            # The waiting lids changed under the photo: the odds again, quickly
            out["candidates"] = [c for c in candidates(f)]
            self._match(out)
        self._last = out
        keep, self._keep_pick = self._keep_pick, False
        res, match = out["res"], out["match"]
        best = match.best
        warnings = res.warnings + match.warnings
        if res.lid is None or best is None:
            f.show_odds({}, None)
            text = _("📷 No lid found.") if res.lid is None else _("📷 Nothing waiting matches.")
            f.set_camera_prompt(text + " " + " ".join(warnings), "#b26b00")
            self.view.live(res.annotated, text, "#ffcc66")
            return
        odds = {}
        for gm in match.groups:
            for c in gm.group.members:
                odds[(c["tote_key"], c["row_key"])] = gm.probability
        member = best.group.first
        # A retake keeps the engraver's pick; otherwise the likeliest is picked
        f.show_odds(odds, None if keep else (member["tote_key"], member["row_key"]))
        if self.sure(out):
            f.set_camera_prompt(_("📷 %s - %d%% sure. Check the preview, then Confirm.")
                                % (LidMatch.describe_group(best.group), round(100 * best.probability)),
                                "#1a7f37")
        else:
            f.set_camera_prompt(_("📷 Not sure which lid this is. Pick it in the list if the "
                                  "selected one is wrong, then Save it to the library."), "#b26b00")
        if warnings:
            f.warn_var.set("\n".join(warnings))
        if not rematch:
            f._status(_("📷 Looks like %s - %d%% sure.") % (LidMatch.describe_group(best.group),
                                                            round(100 * best.probability)))
        f.camera_ready()

    def sure(self, out=None, tote=None, i=None):
        """Whether the camera is sure enough to pick the lid by itself: of a
        colour it has named lids of, and SURE_P likely. With `tote`/`i`, also
        that this is the lid it picked."""
        out = out or self._fresh()
        best = out["match"].best if out is not None else None
        if best is None or best.colour_from != "named lids" or best.probability < SURE_P:
            return False
        if tote is None:
            return True
        key = (tote.key, tote.row_keys[i])
        return any((c["tote_key"], c["row_key"]) == key for c in best.group.members)

    def lid_on_bed(self):
        """An identified lid sits on the bed, not yet engraved."""
        return enabled() and self.state == "on" and self._fresh() is not None

    # =============================================================== panel
    def _library_crop(self, lid, colour):
        """The newest named lid of this lid and colour (else of this colour):
        (BGR crop, what it is) or (None, None)."""
        L = self._learning
        if L is None or not colour:
            return None, None
        try:
            import cv2 as cv
        except ImportError:
            return None, None
        with L.lock:
            examples = list(L.store.examples)
        for same_lid in (True, False):
            for e in reversed(examples):
                if e.colour != colour or (same_lid and e.size != lid) or e.measured is None:
                    continue
                crop = cv.imread(L.store._p("crops", e.id, ".png"))
                if crop is not None:
                    return crop, e.size
        return None, None

    def show_panel(self, tote, i):
        """The panel beside the preview of row `i` of `tote` (just made): this
        lid's photo above the library's, and Retake / Save to Library when
        the engraver picked the lid rather than the camera."""
        out = self._fresh()
        if out is None:
            self.view.hide_all()
            return
        row = tote.rows[i]
        lid = self.frame._lid_for(tote, i)
        colour = (row.get("colour_code") or "").upper()
        name = "%s, %s" % (row.get("colour_name") or colour or "?", lid or "?")
        library, library_lid = self._library_crop(lid, colour)
        if library is None:
            library_caption = _("In the library: nothing yet")
        elif library_lid == lid:
            library_caption = _("In the library: %s") % name
        else:
            library_caption = _("In the library: %s, %s (no %s yet)") % (
                row.get("colour_name") or colour, library_lid, lid)
        best = out["match"].best
        p = round(100 * best.probability) if best is not None else 0
        if self.sure(out, tote, i):
            self.view.panel(_("%s  ·  %d%% sure") % (name, p), "#7ee787", out["crop"], library,
                            library_caption, None)
            return
        if out.get("saved") == (tote.key, tote.row_keys[i]):
            note = _("✓ Saved to the library as %s.") % name
        elif best is not None and self.sure(out):
            note = _("The camera thought this was %s. Picked by hand: Save the photo to "
                     "teach the camera, or Retake it first.") % LidMatch.describe_group(best.group)
        else:
            note = _("Picked by hand: Save the photo to teach the camera this lid, or "
                     "Retake it first (lid flat in the fixture, fully in view).")
        actions = [(_("📷 Retake"), self.retake)]
        if out.get("saved") != (tote.key, tote.row_keys[i]):
            actions.append((_("Save to Library"), lambda: self.save_pick(tote, i)))
        self.view.panel(name, "white", out["crop"], library, library_caption, note, actions)

    def retake(self):
        """A new photo of the lid on the bed, keeping the engraver's pick."""
        self._keep_pick = True
        self.look(force=True)
        if self._job is None:       # no photo taken: nothing will clear it
            self._keep_pick = False

    def save_pick(self, tote, i):
        """Keep the photo of the lid on the bed as row `i` of `tote`."""
        out = self._fresh()
        if out is None:
            self.frame._warn(_("No photo of the lid on the bed to save - Retake it."))
            return
        self._teach_row(out, tote, i)
        out["saved"] = (tote.key, tote.row_keys[i])
        self.frame._status(_("📷 Saved to the library."))
        self.show_panel(tote, i)

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

    def _fresh(self):
        """The identified lid still on the bed, if it was found."""
        out = self._last
        if out is None or out["res"].lid is None or time.time() - out["time"] > TEACH_BY_HAND_S:
            return None
        return out

    def note_engraving(self, tote, i):
        """The frame is about to engrave row `i` of `tote`, confirmed by the
        engraver. A lid the camera picked by itself is kept as that lid; one
        picked by hand is kept only by Save to Library."""
        out = self._fresh()
        if out is not None and self.sure(out, tote, i):
            self._teach_row(out, tote, i)

    def _teach_row(self, out, tote, i):
        if out.get("taught"):
            return
        row = tote.rows[i]
        lid = self.frame._lid_for(tote, i)
        defaults = getattr(self.frame._gen(), "_lid_defaults", {}) or {}
        size = PillcaseOrder.lid_outline(defaults.get(lid)) if lid else None
        self._teach(out, LidMatch.shape_key(size), (row.get("colour_code") or "").upper(), lid,
                    tote.key, tote.row_keys[i])

    # ============================================================= preview
    def show_backdrop(self, lid, frame_mm):
        """The photo of the lid on the bed, faintly under the preview of its
        engraving on `lid` (frame `frame_mm`); nothing without one."""
        canvas = getattr(getattr(self.app, "canvasFrame", None), "canvas", None)
        if canvas is None or not hasattr(canvas, "setLidBackdrop"):
            return
        out = self._fresh()
        image = None
        if out is not None and enabled() and backdrop_on() and frame_mm and None not in frame_mm:
            try:
                from PIL import Image
                res = out["res"]
                flat = LidVision.flat_lid(res, load_corner())
                if flat is not None:
                    image = Image.fromarray(backdrop_image(
                        flat, res.ppm, res.lid.length_is_min, frame_mm, backdrop_corner()), "RGBA")
            except Exception as e:
                print("[lidcam] no lid photo under the preview:", repr(e))
        canvas.setLidBackdrop(image, frame_mm if image is not None else (0, 0))

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
        watch = BooleanVar(value=watching())
        backdrop = BooleanVar(value=backdrop_on())
        corner_at = StringVar(value=backdrop_corner())
        mm = StringVar(value="%g" % marker_mm())
        scale = StringVar(value="%.4f" % size_scale())
        msg = StringVar()

        Checkbutton(body, text=_("Use the lid camera"), variable=on).grid(
            row=0, column=0, columnspan=2, sticky=W)
        Checkbutton(body, text=_("Watch for a lid being placed, and identify it"),
                    variable=watch).grid(row=1, column=0, columnspan=2, sticky=W)
        bd = Frame(body)
        bd.grid(row=2, column=0, columnspan=2, sticky=W)
        Checkbutton(bd, text=_("Show the lid's photo under its preview, fixture corner at the"),
                    variable=backdrop).pack(side=LEFT)
        ttk.Combobox(bd, textvariable=corner_at, values=BACKDROP_CORNERS, state="readonly",
                     width=12).pack(side=LEFT, padx=(4, 0))
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
            tote, i, row_ = f._selected()
            lid = f._lid_for(tote, i) if row_ is not None else None
            defaults = getattr(f._gen(), "_lid_defaults", {}) or {}
            want = PillcaseOrder.lid_outline(defaults.get(lid)) if lid else None
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
        Label(learnf, textvariable=learn_state, anchor=W, justify=LEFT,
              font="TkFixedFont").pack(side=TOP, fill=X)

        def describe_learning():
            L = self._learning
            if L is None:
                return
            ex = L.store.examples
            m, hit = LidMatch.best_colour_method(ex, L.network)
            lines = [_("Named lids: %d, %d colours. Image network: %s.") % (
                len(ex), len({e.colour for e in ex}),
                _("in use") if L.network is not None else _("not downloaded yet"))]
            ranking = LidMatch.method_ranking(ex, L.network)
            if ranking:
                lines.append(_("Right first time, on lids of a colour named before:"))
                for method, right, n in ranking:
                    lines.append("  %s %-18s %3d%%  (%d of %d)" % (
                        "▶" if method == m else " ", LidMatch.METHOD_NAMES.get(method, method),
                        round(100 * right / n), right, n))
            else:
                lines.append(_("No method tested yet: a lid tests them once its colour has "
                               "been named before."))
            lines.append(_("In use: %s (switches only when another is right %d%% more often "
                           "over %d lids).") % (LidMatch.METHOD_NAMES.get(m, m),
                                                round(100 * LidMatch.METHOD_MARGIN),
                                                LidMatch.METHOD_MIN))
            learn_state.set("\n".join(lines))

        def load_learning():
            self._start(self.learning, lambda L: describe_learning(),
                        lambda e: learn_state.set(_("Named lids: could not load (%s)") % e))

        if self._job is None:
            load_learning()

        def forget_colours():
            if messagebox.askyesno(_("Forget Colours"),
                                   _("Forget the colours averaged from confirmed lids? The named "
                                     "lids are kept."), parent=d):
                self.colours.forget()
                msg.set(_("Averaged colours cleared."))

        row = Frame(learnf)
        row.pack(side=TOP, fill=X, pady=(4, 0))
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
            Utils.setBool(_SECTION, "watch", watch.get())
            Utils.setBool(_SECTION, "backdrop", backdrop.get())
            Utils.setStr(_SECTION, "backdropCorner", corner_at.get())
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
