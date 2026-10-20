#!/usr/bin/env python3
"""Teach the lid camera: name each lid it sees, and watch how fast it learns.

Place a lid, press Capture (Space). Before you answer, every method guesses
from the lids named so far (bCNC/LidLearn.py says what each one looks at), and
so does the current thumbnail matching, for comparison. Then pick the lid's
size and colour and Save (Enter). The guesses are kept with your answer, so the
chart below the photo shows what the camera would really have said: how often
each method was right the first time, by how many lids of that colour had been
named before. Nothing is preselected, so a guess cannot steer the answer.

Two more methods learn from the named lids themselves: "Learned weights" (how
much colour, palette, pattern and network should each count) and "Trained
layer" (a small classifier). They start once 30 lids are named and refit every
10 after that, in the background, from the lids named before only. The line
under the guesses says what they last learned.

Use different physical lids of a colour where you can: naming the same lid
five times teaches the camera that one lid, not the colour.

    python tools/lidcam_learn.py                         # camera, auto-finds the board
    python tools/lidcam_learn.py --port /dev/ttyACM1
    python tools/lidcam_learn.py --images ~/lidcam/saved  # name saved photos instead
    python tools/lidcam_learn.py --report                 # recompute everything, as text
    python tools/lidcam_learn.py --delete <id>            # drop a wrongly named lid
    python tools/lidcam_learn.py --remeasure              # outlines again, e.g. after a new empty bed

It holds the probe's serial port, like lidcam_viewer.py: close bCNC (or its
BLTouch port) first. Named lids go to --examples (default ~/lidcam/examples).
The empty-bed photo is shared with lidcam_viewer.py (in --out). The image
network (14 MB) is downloaded on first use into ~/lidcam/models; --no-network
runs without it.
"""

import argparse
import glob
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bCNC"))
sys.path.insert(0, HERE)

import cv2 as cv  # noqa: E402

import LidLearn  # noqa: E402
import LidVision  # noqa: E402
import PillcaseOrder  # noqa: E402
import SkuParser  # noqa: E402
import lidcam_snapshot as snap  # noqa: E402
import lidcam_viewer as viewer  # noqa: E402

METHOD_NAMES = {
    "colour": "Colour",
    "palette": "Palette + pattern",
    "network": "Network",
    "all": "Network + palette",
    "weighted": "Learned weights",
    "trained": "Trained layer",
    "thumbnail": "Thumbnail (current)",
    "size_outline": "Outline",
    "size_learned": "Learned",
}
# Dark-surface categorical steps, validated on #2a2a2a (dataviz validate_palette).
# Green is under 3:1 on it, so every line also carries a text label (and
# --report has the same numbers as a table).
SERIES = {"colour": "#3987e5", "palette": "#d95926", "network": "#199e70", "all": "#c98500",
          "weighted": "#d55181", "trained": "#008300"}
BASELINE = "#8d8d8d"
SURFACE = "#2a2a2a"
INK, INK_DIM, GRID = "#e8e8e8", "#a0a0a0", "#3a3a3a"


def colour_choices():
    """[(label, code)] for every colour the SKU parser knows, by name."""
    out = []
    for code, name in SkuParser.COLOUR_NAMES.items():
        if code == "UNKN":
            continue
        out.append(("%s  (%s)" % (name, code) if name else code, code))
    return sorted(out, key=lambda r: r[0].lower())


def thumbnail_codes():
    """Thumbnail file stem -> colour code, so the current method's guesses can
    be scored against what the operator names."""
    stems = {os.path.splitext(f)[0] for f in os.listdir(snap.THUMBS) if f.endswith(".png")}
    out = {}
    for code, name in SkuParser.COLOUR_NAMES.items():
        for base in PillcaseOrder.thumbnail_basenames(code, name):
            if base in stems and base not in out:
                out[base] = code
    return out


class FileWorker(viewer.Worker):
    """Saved photos instead of the camera, one each time `advance` is set."""

    def __init__(self, files, marker_mm, out_dir, results):
        super().__init__("saved photos", marker_mm, out_dir, results)
        self.files = list(files)
        self.advance = threading.Event()
        self.advance.set()

    def run(self):
        while not self.stopped.is_set():
            if not self.advance.wait(0.1):
                continue
            self.advance.clear()
            if self.reload_background.is_set():
                self.reload_background.clear()
                self._load_background()
            if not self.files:
                self.results.put(("done",))
                continue
            path = self.files.pop(0)
            with open(path, "rb") as f:
                jpeg = f.read()
            img = LidVision.decode_jpeg(jpeg)
            t = time.time()
            res = LidVision.identify(jpeg, self.candidates, self.ref, self.marker_mm,
                                     background=self.background, corner=self.corner)
            shot = SimpleNamespace(jpeg=jpeg, width=img.shape[1], height=img.shape[0], path=path)
            self.results.put(("frame", shot, res, 0.0, time.time() - t))


class Learner(viewer.Viewer):
    """The window. Borrows the live viewer's fonts, image panels and empty-bed
    buttons; everything about naming lids is its own."""

    def __init__(self, root, worker, out_dir, marker_mm, font_size, store, network,
                 file_mode=False, auto_labels=None):
        self.root = root
        self._setup_fonts(font_size)
        self.worker = worker
        self.out_dir = out_dir
        self.marker_mm = marker_mm
        self.results = worker.results
        self.store = store
        self.network = network
        self.file_mode = file_mode
        self.auto_labels = auto_labels
        self.outlines = LidLearn.load_outlines(snap.OUTLINES)
        self.thumb_codes = thumbnail_codes()
        self.colours = colour_choices()
        self.colour_label = {code: label for label, code in self.colours}
        self.pair_cache = {}
        self.trainer = LidLearn.Trainer(self.outlines, self.pair_cache)
        self.busy = False               # re-measuring the named lids
        self.remeasured = queue.Queue()
        self.last = None            # (shot, res) on show
        self.frozen = None          # the capture being named: dict, or None
        self.analysis = queue.Queue()
        self.frames = 0
        self.hover_points = []

        root.title("Lid Camera - teach")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("%dx%d" % (int(sw * 0.92), int(sh * 0.88)))
        root.configure(bg="#202020")

        bar = tk.Frame(root, bg="#202020")
        bar.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)
        self.capture_btn = ttk.Button(bar, text="Capture (Space)", command=self.capture)
        self.capture_btn.pack(side=tk.LEFT)
        self.save_btn = ttk.Button(bar, text="Save (Enter)", command=self.save)
        self.save_btn.pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Skip next photo (Esc)" if file_mode else "Retake (Esc)",
                   command=self.retake).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Undo last", command=self.undo).pack(side=tk.LEFT, padx=(16, 4))
        if not file_mode:
            ttk.Button(bar, text="Save Empty Bed (no lid)", command=self.save_empty_bed).pack(
                side=tk.LEFT, padx=(16, 4))
            ttk.Button(bar, text="Clear Empty Bed", command=self.clear_empty_bed).pack(side=tk.LEFT, padx=4)
            ttk.Button(bar, text="Set Lid Corner", command=self.set_lid_corner).pack(
                side=tk.LEFT, padx=(16, 4))
            ttk.Button(bar, text="Calibrate Exposure", command=self.calibrate_exposure).pack(
                side=tk.LEFT, padx=(16, 4))
        self.status = tk.StringVar(value="Loading named lids...")
        tk.Label(bar, textvariable=self.status, fg=INK, bg="#202020",
                 font=self.f_text).pack(side=tk.LEFT, padx=12)

        body = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashwidth=6, bg="#202020")
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        left = tk.PanedWindow(body, orient=tk.VERTICAL, sashwidth=6, bg="#202020")
        self.cam = self._panel(left, "Camera")
        chart_box = tk.Frame(left, bg=SURFACE)
        self.chart = tk.Canvas(chart_box, bg=SURFACE, highlightthickness=0, height=260)
        self.chart.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=(6, 0))
        self.summary = tk.StringVar(value="")
        tk.Label(chart_box, textvariable=self.summary, fg=INK_DIM, bg=SURFACE, anchor="w",
                 justify=tk.LEFT, font=self.f_small).pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 6))
        self.chart.bind("<Configure>", lambda e: self.draw_chart())
        self.chart.bind("<Motion>", self._hover)
        self.chart.bind("<Leave>", lambda e: self.chart.delete("tip"))
        win_h = int(sh * 0.88)
        left.add(self.cam[0], stretch="always", minsize=200, height=int(win_h * 0.6))
        left.add(chart_box, minsize=180)

        right = tk.Frame(body, bg=SURFACE)
        self._build_right(right)
        win_w = int(sw * 0.92)
        body.add(left, stretch="always", minsize=400, width=int(win_w * 0.64))
        body.add(right, minsize=320, width=int(win_w * 0.36))

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<space>", self._key(self.capture))
        root.bind("<Return>", lambda e: self.save())
        root.bind("<KP_Enter>", lambda e: self.save())
        root.bind("<Escape>", lambda e: self.retake())
        for i in range(10):
            root.bind(str(i), self._key(lambda i=i: self._pick_size((i - 1) % 10)))
        for seq in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            root.bind(seq, lambda e: self.zoom(+1))
        for seq in ("<Control-minus>", "<Control-KP_Subtract>"):
            root.bind(seq, lambda e: self.zoom(-1))
        root.bind("<Control-0>", lambda e: self.zoom(0))
        self._set_capture_state()
        self.refresh_stats()
        self._learn_ahead()
        root.after(50, self.poll)

    # ----------------------------------------------------------- layout
    def _build_right(self, right):
        def head(text):
            tk.Label(right, text=text, fg="#ffffff", bg=SURFACE, anchor="w",
                     font=self.f_head).pack(side=tk.TOP, fill=tk.X, padx=8, pady=(8, 2))

        head("What the camera measured")
        # A fixed height in pixels: a Label sized in text lines collapses to a
        # sliver once it holds an image.
        crop_box = tk.Frame(right, bg="#111111", height=120)
        crop_box.pack(side=tk.TOP, fill=tk.X, padx=8)
        crop_box.pack_propagate(False)
        self.crop_lbl = tk.Label(crop_box, bg="#111111", font=self.f_text)
        self.crop_lbl.pack(fill=tk.BOTH, expand=True)
        self.guess = tk.Text(right, height=16, bg=SURFACE, fg=INK, bd=0, highlightthickness=0,
                             font=self.f_mono, wrap=tk.WORD)
        self.guess.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(4, 0))
        self.guess.tag_configure("head", font=self.f_head, foreground="#ffffff", spacing1=6)
        self.guess.tag_configure("dim", foreground=INK_DIM)
        self.guess.tag_configure("good", foreground="#6fdc8c")
        self.guess.tag_configure("mid", foreground="#f1c21b")
        self.guess.tag_configure("bad", foreground="#ff8389")

        head("Size  (keys 1-9, 0)")
        self.size_list = tk.Listbox(right, height=10, exportselection=False, bg="#1e1e1e", fg=INK,
                                    selectbackground="#3987e5", font=self.f_text,
                                    highlightthickness=0, bd=0, activestyle="none")
        self.size_list.pack(side=tk.TOP, fill=tk.X, padx=8)
        other = tk.Frame(right, bg=SURFACE)
        other.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(4, 0))
        tk.Label(other, text="Not listed:", fg=INK_DIM, bg=SURFACE, font=self.f_small).pack(side=tk.LEFT)
        self.size_other = tk.StringVar()
        ttk.Entry(other, textvariable=self.size_other, font=self.f_text).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        self._fill_sizes()

        head("Colour  (type to filter)")
        self.colour_filter = tk.StringVar()
        self.filter_entry = ttk.Entry(right, textvariable=self.colour_filter, font=self.f_text)
        self.filter_entry.pack(side=tk.TOP, fill=tk.X, padx=8)
        self.colour_filter.trace_add("write", lambda *a: self._fill_colours())
        self.filter_entry.bind("<Down>", lambda e: (self.colour_list.focus_set(),
                                                    self._select_colour_row(0)))
        self.colour_list = tk.Listbox(right, exportselection=False, bg="#1e1e1e", fg=INK,
                                      selectbackground="#3987e5", font=self.f_text,
                                      highlightthickness=0, bd=0, activestyle="none")
        self.colour_list.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))
        self._fill_colours()

    def _fill_sizes(self):
        names = sorted(self.outlines, key=lambda n: (self.outlines[n][0] * self.outlines[n][1], n))
        names += sorted({e.size for e in self.store.examples} - set(names))
        self.size_names = names
        self.size_list.config(height=min(len(names), 14))
        self.size_list.delete(0, tk.END)
        for i, n in enumerate(names):
            key = str((i + 1) % 10) if i < 10 else " "
            w, l = self.outlines.get(n, (None, None))
            dims = "   %.0f x %.0f" % (w, l) if w else ""
            self.size_list.insert(tk.END, "%s  %s%s" % (key, n, dims))

    def _fill_colours(self):
        text = self.colour_filter.get().strip().lower()
        keep = self._colour_selected()
        self.colour_rows = [(label, code) for label, code in self.colours if text in label.lower()]
        self.colour_list.delete(0, tk.END)
        for label, _code in self.colour_rows:
            self.colour_list.insert(tk.END, label)
        # One match left: that is the answer, no need to also click it.
        if len(self.colour_rows) == 1:
            self._select_colour_row(0)
        elif keep:
            for i, (_l, code) in enumerate(self.colour_rows):
                if code == keep:
                    self._select_colour_row(i)

    def _select_colour_row(self, i):
        if 0 <= i < len(self.colour_rows):
            self.colour_list.selection_clear(0, tk.END)
            self.colour_list.selection_set(i)
            self.colour_list.activate(i)
            self.colour_list.see(i)

    def _colour_selected(self):
        sel = self.colour_list.curselection() if hasattr(self, "colour_rows") else ()
        return self.colour_rows[sel[0]][1] if sel else None

    def _size_selected(self):
        typed = self.size_other.get().strip()
        if typed:
            return typed
        sel = self.size_list.curselection()
        return self.size_names[sel[0]] if sel else None

    def _pick_size(self, i):
        if self.frozen and 0 <= i < len(self.size_names):
            self.size_list.selection_clear(0, tk.END)
            self.size_list.selection_set(i)
            self.size_list.see(i)

    def _key(self, fn):
        """A single-key shortcut that stays out of the way while typing."""
        def handler(event):
            if isinstance(event.widget, (tk.Entry, ttk.Entry)):
                return None
            fn()
            return "break"
        return handler

    def _clear_answer(self):
        self.size_list.selection_clear(0, tk.END)
        self.size_other.set("")
        self.colour_filter.set("")
        self.colour_list.selection_clear(0, tk.END)

    def _set_capture_state(self):
        frozen = self.frozen is not None
        self.capture_btn.state(["disabled"] if frozen or self.file_mode or self.busy
                               else ["!disabled"])
        self.save_btn.state(["!disabled"] if frozen and "guesses" in self.frozen else ["disabled"])

    # ----------------------------------------------------------- capture
    def capture(self):
        if self.frozen is not None or self.last is None or self.file_mode or self.busy:
            return
        self.worker.paused.set()
        self._freeze(*self.last)

    def _freeze(self, shot, res):
        self.frozen = {"shot": shot, "res": res}
        self._clear_answer()
        self._show(self.cam[1], res.annotated, "")
        self.guess.delete("1.0", tk.END)
        self.guess.insert(tk.END, "Looking...\n", "dim")
        self._set_capture_state()
        examples = list(self.store.examples)
        threading.Thread(target=self._analyse, args=(shot, res, examples), daemon=True).start()

    def _analyse(self, shot, res, examples):
        """Crop, features and every method's guess, off the Tk thread."""
        out = {"res": res, "crop": None, "mask": None, "feats": None}
        try:
            usable = [e for e in examples if e.features is not None]
            mem = LidLearn.Memory(usable, self.outlines, self.pair_cache)
            # Step 2, as fitted on the lids named so far (refits every
            # REFIT_EVERY lids; usually already done in the background).
            learned = out["learned"] = self.trainer.get(usable)
            if res.lid is not None:
                out["crop"], out["mask"] = LidLearn.lid_crop(res.rectified, res.lid, res.ppm)
                out["feats"] = LidLearn.features(out["crop"], out["mask"], self.network)
            out["ranks"] = LidLearn.rank_all(mem, out["feats"], learned)
            out["n_usable"] = len(usable)
            lid = res.lid
            out["size_ranks"] = {m: mem.rank_sizes(m, lid.size_mm, lid.width_is_min,
                                                   lid.length_is_min) if lid else []
                                 for m in LidLearn.SIZE_METHODS}
            g = LidLearn.guesses(mem, out["feats"], lid, learned, out["ranks"])
            best = res.best
            g["thumbnail"] = None
            if best is not None:
                code = self.thumb_codes.get(best.first["colour_key"])
                g["thumbnail"] = [code, round(best.probability, 3)] if code else None
            out["guesses"] = g
        except Exception as e:          # shown, not raised: one bad photo must not end a session
            out["error"] = repr(e)
        self.analysis.put(out)

    def _show_analysis(self, out):
        if self.frozen is None or self.frozen["res"] is not out["res"]:
            return                          # retaken meanwhile
        self.frozen.update(out)
        t = self.guess
        t.delete("1.0", tk.END)
        res = out["res"]
        if out.get("error"):
            t.insert(tk.END, "Could not analyse this photo: %s\n" % out["error"], "bad")
            self._set_capture_state()
            return
        if out["crop"] is not None:
            self._show(self.crop_lbl, out["crop"], "")
        else:
            self.crop_lbl.config(image="", text="no lid found", fg=INK_DIM)
            self.crop_lbl.image = None
        if res.lid is not None:
            t.insert(tk.END, "%s" % LidVision.describe_size(res.lid))
            t.insert(tk.END, "  (found by %s)\n" % ("empty-bed difference"
                                                  if res.lid.method == "background" else "edges"), "dim")
        else:
            t.insert(tk.END, "No lid found. ", "bad")
            t.insert(tk.END, "Retake, or name it and Save to count it as a missed lid.\n", "dim")
        for w in res.warnings:
            if "No lid found" not in w:
                t.insert(tk.END, w + "\n", "mid")
        t.insert(tk.END, "Size\n", "head")
        for m in LidLearn.SIZE_METHODS:
            r = out["size_ranks"].get(m) or []
            t.insert(tk.END, "  %-11s" % METHOD_NAMES["size_" + m], "dim")
            t.insert(tk.END, ("%s  %s\n" % (r[0][0], self._pct(r[0][2]))) if r else "-\n")
        t.insert(tk.END, "Colour\n", "head")
        n_named = len(self.store.examples)
        for m in LidLearn.COLOUR_METHODS:
            r = out["ranks"].get(m) or []
            t.insert(tk.END, "  %-21s" % METHOD_NAMES[m], "dim")
            if r:
                for i, (code, _d, p) in enumerate(r[:2]):
                    t.insert(tk.END, ("%s %s" % (self._short(code), self._pct(p))),
                             self._conf_tag(p) if i == 0 else "dim")
                    t.insert(tk.END, "   " if i == 0 else "")
                t.insert(tk.END, "\n")
            elif m in ("network", "all") and self.network is None:
                t.insert(tk.END, "off (no network)\n", "dim")
            elif out["feats"] is None:
                t.insert(tk.END, "-\n", "dim")
            elif m in LidLearn.LEARNED_METHODS and m not in out["ranks"]:
                t.insert(tk.END, self._step2_wait(out, m) + "\n", "dim")
            else:
                t.insert(tk.END, "nothing named yet\n" if n_named == 0 else "-\n", "dim")
        g = out["guesses"].get("thumbnail")
        t.insert(tk.END, "  %-21s" % METHOD_NAMES["thumbnail"], "dim")
        t.insert(tk.END, ("%s %s\n" % (self._short(g[0]), self._pct(g[1]))) if g else "-\n")
        if out.get("learned") is not None:
            t.insert(tk.END, out["learned"].describe() + "\n", "dim")
        self._set_capture_state()
        if self.auto_labels is not None:
            self._auto_answer()
        else:
            self.filter_entry.focus_set()

    @staticmethod
    def _step2_wait(out, m):
        """Why a step-2 method has no guess yet."""
        n = out.get("n_usable", 0)
        if n < LidLearn.LEARN_FROM:
            return "starts at %d lids (%d so far)" % (LidLearn.LEARN_FROM, n)
        if m == "weighted":
            return "needs %d lids that share a colour with another" % LidLearn.MIN_PAIRED
        return "needs 2 colours"

    def save_empty_bed(self):
        if self.frozen is not None or self.busy:
            self.status.set("Save or retake the lid first.")
            return False
        if not super().save_empty_bed():
            return False
        self._offer_remeasure("this empty-bed photo")
        return True

    def set_lid_corner(self):
        if self.frozen is not None or self.busy:
            self.status.set("Save or retake the lid first.")
            return False
        if not super().set_lid_corner():
            return False
        self._offer_remeasure("this fixture corner")
        return True

    def _offer_remeasure(self, what):
        n = len(self.store.examples)
        if n and messagebox.askyesno(
                "Re-measure named lids",
                "Measure the %d named lids again with %s?\n\n"
                "Their outlines, sizes and crops are redone from their photos. Your answers "
                "are kept. Takes about %d seconds." % (n, what, max(2, n // 2)), parent=self.root):
            self._remeasure()

    def _remeasure(self):
        """Remeasure on a thread; the window waits (no captures) meanwhile."""
        self.busy = True
        self._set_capture_state()
        self.status.set("Re-measuring the named lids...")

        def work():
            try:
                lines = []
                remeasure(self.store, self.out_dir, self.marker_mm, log=lines.append)
                self.store.load_features(self.network)
                self.remeasured.put(("done", lines))
            except Exception as e:
                self.remeasured.put(("error", e))
        threading.Thread(target=work, daemon=True).start()

    def _remeasure_done(self, kind, value):
        self.busy = False
        # Everything learned from the old crops goes: distances, step 2.
        self.pair_cache.clear()
        self.trainer = LidLearn.Trainer(self.outlines, self.pair_cache)
        self._learn_ahead()
        self.refresh_stats()
        self._set_capture_state()
        if kind == "error":
            self.status.set("Re-measuring failed: %s" % value)
            return
        for line in value:
            print(line)
        missed = sum(1 for e in self.store.examples if e.measured is None)
        self.status.set("Re-measured %d named lids%s." % (
            len(self.store.examples), ", %d not found" % missed if missed else ""))

    def calibrate_exposure(self):
        if self.frozen is not None:
            self.status.set("Save or retake the lid first, then calibrate.")
            return
        super().calibrate_exposure()

    def _learn_ahead(self):
        """Refit step 2 in the background when a save crosses a refit point,
        so the next capture does not wait for it."""
        usable = [e for e in self.store.examples if e.features is not None]
        if LidLearn.fit_point(len(usable)):
            threading.Thread(target=self.trainer.get, args=(usable,), daemon=True).start()

    def _short(self, code):
        label = self.colour_label.get(code, code or "?")
        return label.split("  (")[0]

    @staticmethod
    def _pct(p):
        return "%d%%" % round(p * 100)

    @staticmethod
    def _conf_tag(p):
        return "good" if p >= 0.8 else "mid" if p >= 0.5 else "bad"

    # -------------------------------------------------------------- save
    def save(self):
        f = self.frozen
        if f is None or "guesses" not in f:
            return
        size, colour = self._size_selected(), self._colour_selected()
        if not size or not colour:
            self.status.set("Pick the lid's size and colour first.")
            return
        res = f["res"]
        try:
            self.store.add(f["shot"].jpeg, f["crop"], f["mask"], res.lid, size, colour,
                           f["guesses"], f["feats"],
                           setup={"marker_mm": self.marker_mm,
                                  "empty_bed": self.worker.background is not None})
        except Exception as e:      # shown: a save that fails quietly looks like a dead button
            self.status.set("NOT SAVED - %s: %s" % (type(e).__name__, e))
            messagebox.showerror("Not saved", "This lid was not saved:\n\n%r" % e, parent=self.root)
            return
        if size not in self.size_names:
            self._fill_sizes()
        g = f["guesses"]
        right = [METHOD_NAMES[m] for m in LidLearn.COLOUR_METHODS
                 if g.get(m) and g[m][0] == colour]
        self.status.set("Saved %s, %s.  Right: %s" % (size, self._short(colour),
                                                      ", ".join(right) if right else "none"))
        self.refresh_stats()
        self._learn_ahead()
        self._next()

    def retake(self):
        if self.frozen is None:
            return
        self.status.set("Skipped." if self.file_mode else "Not saved.")
        self._next()

    def _next(self):
        self.frozen = None
        self._clear_answer()
        self.crop_lbl.config(image="", text="")
        self.crop_lbl.image = None
        self.guess.delete("1.0", tk.END)
        self._set_capture_state()
        if self.file_mode:
            self.worker.advance.set()
        else:
            self.worker.paused.clear()
        self.root.focus_set()

    def undo(self):
        if not self.store.examples:
            return
        ex = self.store.examples[-1]
        if not messagebox.askyesno("Undo last", "Delete the last named lid (%s, %s)?"
                                   % (ex.size, self._short(ex.colour)), parent=self.root):
            return
        self.store.remove(ex.id)
        self.pair_cache.clear()
        self.status.set("Deleted %s." % ex.id)
        self.refresh_stats()

    def _auto_answer(self):
        """For testing: answer from a labels file, as an operator would."""
        path = getattr(self.frozen["shot"], "path", "")
        size, colour = self.auto_labels.get(os.path.basename(path), [None, None])[:2]
        if size is None:
            self.retake()
            return
        self.size_other.set(size if size not in self.size_names else "")
        if size in self.size_names:
            self.size_list.selection_set(self.size_names.index(size))
        self.colour_filter.set(self.colour_label.get(colour, colour))
        self.root.after(10, self.save)

    # ------------------------------------------------------------- stats
    def _rows(self):
        """One row per named lid, from the guesses made at the time."""
        rows, seen = [], {}
        for e in self.store.examples:
            g = e.rec.get("guesses") or {}
            row = {"truth": e.colour, "seen": seen.get(e.colour, 0), "missed": e.measured is None}
            for m in LidLearn.COLOUR_METHODS + ("thumbnail",):
                v = g.get(m)
                ran = g.get("ran")
                # "thumbnail" always runs; a method missing from `ran` could
                # not (no network, step 2 not started) and is not counted.
                off = m != "thumbnail" and ran is not None and m not in ran
                row[m] = None if row["missed"] or off else bool(v) and v[0] == e.colour
            for m in LidLearn.SIZE_METHODS:
                v = g.get("size_" + m)
                if row["missed"]:
                    row["size_" + m] = None
                elif m == "outline":
                    row["size_" + m] = bool(v) and LidLearn.size_matches(v[0], e.size)
                else:
                    row["size_" + m] = bool(v) and v[0] == e.size
            rows.append(row)
            seen[e.colour] = seen.get(e.colour, 0) + 1
        return rows

    def refresh_stats(self):
        self.rows = self._rows()
        n = len(self.rows)
        colours = len({r["truth"] for r in self.rows})
        missed = sum(1 for r in self.rows if r["missed"])
        parts = ["%d lids named, %d colours" % (n, colours)]
        if missed:
            parts.append("%d not found by the camera" % missed)
        last = self.rows[-20:]

        def rate(m):
            sel = [r[m] for r in last if r.get(m) is not None]
            return "%s %d%%" % (METHOD_NAMES[m], round(100 * sum(sel) / len(sel))) if sel else None

        recent = [x for x in (rate(m) for m in LidLearn.COLOUR_METHODS + ("thumbnail",)) if x]
        sizes = [x for x in (rate("size_" + m) for m in LidLearn.SIZE_METHODS) if x]
        text = " · ".join(parts)
        if recent:
            text += "\nLast %d, colour right: %s" % (len(last), " · ".join(recent))
        if sizes:
            text += "\nLast %d, size right: %s" % (len(last), " · ".join(sizes))
        self.summary.set(text)
        self.draw_chart()

    def draw_chart(self):
        """Right first time, by how many lids of that colour were named before."""
        c = self.chart
        c.delete("all")
        self.hover_points = []
        W, H = c.winfo_width(), c.winfo_height()
        if W < 100 or H < 100:
            return
        rows = getattr(self, "rows", [])
        fs = self.f_small
        line_h = fs.metrics("linespace")
        title_h = self.f_text.metrics("linespace")
        # Margins from the text that has to fit in them, so nothing is clipped
        # at any font size.
        left = fs.measure("100%") + 16
        right = max(fs.measure(METHOD_NAMES[m]) for m in SERIES) + 28
        right = max(right, fs.measure(METHOD_NAMES["thumbnail"]) + 28)
        top = 8 + title_h + 6 + line_h + 10        # title, then the legend row
        bottom = 10 + 2 * line_h
        pw, ph = W - left - right, H - top - bottom
        if pw < 120 or ph < 40:
            return
        legend_y = 8 + title_h + 6 + line_h / 2
        c.create_text(8, 8, anchor="nw", fill=INK, font=self.f_text,
                      text="Colour right first time, by lids of that colour named before")
        buckets = LidLearn.SEEN_BUCKETS
        xs = [left + pw * (i + 0.5) / len(buckets) for i in range(len(buckets))]

        def y_of(v):
            return top + ph * (1 - v)

        for v in (0, 0.5, 1.0):
            y = y_of(v)
            c.create_line(left, y, left + pw, y, fill=GRID)
            c.create_text(left - 6, y, anchor="e", fill=INK_DIM, font=fs, text="%d%%" % (v * 100))
        counts = LidLearn.by_seen(rows, "colour")
        for x, (label, _k, n) in zip(xs, counts):
            c.create_text(x, top + ph + 6, anchor="n", fill=INK_DIM, font=fs, text=label)
            c.create_text(x, top + ph + 6 + line_h, anchor="n", fill=INK_DIM, font=fs,
                          text="n=%d" % n if n else "")
        if not rows:
            c.create_text(left + pw / 2, top + ph / 2, fill=INK_DIM, font=self.f_text,
                          text="Name some lids to see how fast each method learns.")
            return

        # Legend, one row, above the plot.
        lx = 8
        for m in LidLearn.COLOUR_METHODS + ("thumbnail",):
            colour = SERIES.get(m, BASELINE)
            c.create_line(lx, legend_y, lx + 18, legend_y, fill=colour, width=2,
                          dash=(4, 3) if m == "thumbnail" else None)
            t = c.create_text(lx + 24, legend_y, anchor="w", fill=INK_DIM, font=fs,
                              text=METHOD_NAMES[m])
            lx = c.bbox(t)[2] + 18

        ends = []
        for m in ("thumbnail",) + LidLearn.COLOUR_METHODS:
            colour = SERIES.get(m, BASELINE)
            pts = []
            for x, (label, k, n) in zip(xs, LidLearn.by_seen(rows, m)):
                if n:
                    pts.append((x, y_of(k / n), label, k, n))
            if not pts:
                continue
            for a, b in zip(pts, pts[1:]):
                c.create_line(a[0], a[1], b[0], b[1], fill=colour, width=2,
                              dash=(4, 3) if m == "thumbnail" else None)
            for x, y, label, k, n in pts:
                c.create_oval(x - 5, y - 5, x + 5, y + 5, fill=colour, outline=SURFACE, width=2)
                self.hover_points.append((x, y, m, label, k, n))
            ends.append([pts[-1][1], m, pts[-1]])
        # Direct labels at the right end, nudged apart so they never overlap.
        ends.sort()
        for i in range(1, len(ends)):
            ends[i][0] = max(ends[i][0], ends[i - 1][0] + line_h)
        for y, m, p in ends:
            c.create_line(p[0] + 8, p[1], left + pw + 6, y, fill=GRID)
            c.create_text(left + pw + 10, y, anchor="w", fill=INK, font=fs, text=METHOD_NAMES[m])

    def _hover(self, event):
        c = self.chart
        c.delete("tip")
        near = [(abs(x - event.x) + abs(y - event.y), x, y, m, label, k, n)
                for x, y, m, label, k, n in self.hover_points]
        near = [p for p in near if p[0] < 14]
        if not near:
            return
        _d, x, y, m, label, k, n = min(near)
        text = "%s\n%s named before: %d of %d right (%d%%)" % (
            METHOD_NAMES[m], label, k, n, round(100 * k / n))
        t = c.create_text(x + 12, y - 12, anchor="sw", text=text, fill=INK, font=self.f_small,
                          tags="tip")
        x0, y0, x1, y1 = c.bbox(t)
        if x1 > c.winfo_width() - 4:
            c.move(t, -(x1 - x0) - 24, 0)
            x0, y0, x1, y1 = c.bbox(t)
        if y0 < 4:
            c.move(t, 0, 4 - y0)
            x0, y0, x1, y1 = c.bbox(t)
        bg = c.create_rectangle(x0 - 6, y0 - 4, x1 + 6, y1 + 4, fill="#111111",
                                outline=SERIES.get(m, BASELINE), tags="tip")
        c.tag_lower(bg, t)

    # -------------------------------------------------------------- loop
    def poll(self):
        latest = None
        try:
            while True:
                item = self.results.get_nowait()
                if item[0] in ("job", "log"):
                    self._job_item(item)
                elif item[0] == "error":
                    self.status.set("Camera: %s" % item[1])
                elif item[0] == "done":
                    self.status.set("No more photos.")
                    if self.auto_labels is not None:
                        self.close()
                else:
                    latest = item
        except queue.Empty:
            pass
        if latest is not None and self.frozen is None:
            _kind, shot, res, _tc, _tv = latest
            self.last = (shot, res)
            self.frames += 1
            if self.file_mode:
                self.status.set(os.path.basename(shot.path))
                self._freeze(shot, res)
            else:
                self._show(self.cam[1], res.annotated, "")
                if self.frames == 1:
                    self.status.set("Camera on, exposure %s. Place a lid and press Space."
                                    % viewer.exposure_text(self.worker))
        try:
            while True:
                self._show_analysis(self.analysis.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                self._remeasure_done(*self.remeasured.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self.poll)


# ------------------------------------------------------------------- text
def report(store, outlines):
    rows, trainer = LidLearn.replay(store.examples, outlines)
    missed = [e for e in store.examples if e.features is None]
    print("%d named lids, %d with a lid found, %d colours" % (
        len(store.examples), len(rows), len({e.colour for e in store.examples})))
    print("\nColour right first time, by lids of that colour named before")
    print("(recomputed now; each lid guessed only from those named before it)\n")
    head = [label for _lo, _hi, label in LidLearn.SEEN_BUCKETS]
    print("%-20s" % "" + "".join("%9s" % h for h in head))
    for m in LidLearn.COLOUR_METHODS + tuple("size_" + s for s in LidLearn.SIZE_METHODS):
        if m == "size_outline":
            print("\nSize right, by lids of that COLOUR named before")
        cells = []
        for _label, k, n in LidLearn.by_seen(rows, m):
            cells.append("%9s" % ("%d/%d" % (k, n) if n else "-"))
        print("%-20s" % METHOD_NAMES[m] + "".join(cells))
    fits = sorted(trainer.cache.values(), key=lambda f: f.n)
    print("\nStep 2, refitted every %d lids from %d" % (LidLearn.REFIT_EVERY, LidLearn.LEARN_FROM))
    if not fits:
        print("  not started: %d lids with a lid found so far" % len(rows))
    for f in fits:
        print("  " + f.describe())
    print("\nBy colour (Network + palette, or Palette + pattern without a network)")
    method = "all" if any(r.get("all") is not None for r in rows) else "palette"
    by = {}
    for r in rows:
        by.setdefault(r["truth"], []).append(r[method])
    for e in missed:
        by.setdefault(e.colour, [])
    for code in sorted(by):
        v = [x for x in by[code] if x is not None]
        miss = sum(1 for e in missed if e.colour == code)
        seq = "".join("+" if x else "." for x in v)
        print("  %-28s %3d named  %s%s" % (SkuParser.COLOUR_NAMES.get(code) or code, len(v) + miss,
                                          seq[-40:], "   %d not found" % miss if miss else ""))
    print("  + right, . wrong, oldest first. The first of each colour is always wrong:")
    print("  nothing can name a colour it has never been shown.")


def remeasure(store, out_dir, marker_mm, log=print):
    """Outline, size and crop of every named lid again, from its photo."""
    bg = None
    base = os.path.join(out_dir, "empty-bed")
    if os.path.exists(base + ".json"):
        with open(base + ".json") as f:
            bg = json.load(f)
        bg["image"] = cv.imread(base + ".png")
    log("empty-bed photo: %s" % ("%s.png" % base if bg is not None and bg["image"] is not None
                                 else "none (lids found by their edges)"))
    corner = viewer.load_corner(out_dir)
    log("fixture corner: %s" % ("set" if corner is not None else "none"))
    outlines = LidLearn.load_outlines(snap.OUTLINES)
    for ex in list(store.examples):
        path = store.photo_path(ex)
        if not os.path.exists(path):
            log("  %s: no photo, left as it was" % ex.id)
            continue
        with open(path, "rb") as f:
            jpeg = f.read()
        mm = ex.rec.get("marker_mm", marker_mm)
        res = LidVision.identify(jpeg, snap.candidates(), lambda key: None, mm, background=bg,
                                 corner=corner)
        before = ex.measured
        crop = mask = None
        if res.lid is not None:
            crop, mask = LidLearn.lid_crop(res.rectified, res.lid, res.ppm)
        store.remeasure(ex, res.lid, crop, mask)
        true = outlines.get(ex.size)
        log("  %s %-28s %s -> %s%s" % (
            ex.id, ex.size, "%.1f x %.1f" % tuple(before) if before else "not found",
            LidVision.describe_size(res.lid) if res.lid else "NOT FOUND",
            "   (outline %.1f x %.1f)" % true if true else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", help="the XIAO's serial port (default: auto)")
    ap.add_argument("--images", help="name the photos in this folder instead of using the camera")
    ap.add_argument("--examples", default=os.path.expanduser("~/lidcam/examples"),
                    help="folder of named lids (default ~/lidcam/examples)")
    ap.add_argument("--out", default=os.path.expanduser("~/lidcam"),
                    help="folder of the empty-bed photo, shared with lidcam_viewer.py")
    ap.add_argument("--marker-mm", type=float, default=LidVision.DEFAULT_MARKER_MM)
    ap.add_argument("--font-size", type=int, default=11, help="text size in points (default 11)")
    ap.add_argument("--model", help="the image network's .onnx (default: downloaded once)")
    ap.add_argument("--no-network", action="store_true", help="run without the image network")
    ap.add_argument("--report", action="store_true", help="print how well each method learned")
    ap.add_argument("--delete", metavar="ID", help="delete one named lid")
    ap.add_argument("--remeasure", action="store_true",
                    help="measure every named lid again from its photo, with the current "
                         "empty-bed photo (answers and past guesses are kept)")
    ap.add_argument("--auto-labels", help=argparse.SUPPRESS)       # for testing
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    store = LidLearn.Store(a.examples)
    if a.remeasure:
        remeasure(store, a.out, a.marker_mm)
        return
    if a.delete:
        if not any(e.id == a.delete for e in store.examples):
            raise SystemExit("No named lid %s in %s" % (a.delete, a.examples))
        store.remove(a.delete)
        print("deleted", a.delete)
        return

    network = None
    if not a.no_network:
        try:
            path = a.model or LidLearn.fetch_model(os.path.join(os.path.expanduser("~/lidcam"), "models"))
            network = LidLearn.Network(path)
        except Exception as e:
            print("[lidcam] running without the image network:", e)

    def progress(i, n):
        if n > 20 and i % 20 == 0:
            print("\rpreparing named lids %d/%d" % (i, n), end="", flush=True)

    store.load_features(network, progress)
    if len(store.examples) > 20:
        print()
    if a.report:
        report(store, LidLearn.load_outlines(snap.OUTLINES))
        return

    results = queue.Queue()
    if a.images:
        files = sorted(f for ext in ("*.jpg", "*.jpeg", "*.JPG", "*.png")
                       for f in glob.glob(os.path.join(os.path.expanduser(a.images), ext)))
        files = [f for f in files if "annotated" not in os.path.basename(f)]
        if not files:
            raise SystemExit("No photos in %s" % a.images)
        worker = FileWorker(files, a.marker_mm, a.out, results)
    else:
        worker = viewer.Worker(a.port or snap.find_port(), a.marker_mm, a.out, results)
    auto = None
    if a.auto_labels:
        with open(a.auto_labels) as f:
            auto = json.load(f)
    root = tk.Tk()
    Learner(root, worker, a.out, a.marker_mm, a.font_size, store, network,
            file_mode=bool(a.images), auto_labels=auto)
    worker.start()
    root.mainloop()


if __name__ == "__main__":
    main()
