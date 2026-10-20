#!/usr/bin/env python3
"""Live view of the lid camera, with what the vision makes of each photo.

A window showing, refreshed as fast as the camera delivers (about two photos a
second):

  * the camera's photo, with the markers it found (orange), the lid outline
    (green, or yellow when the lid runs off the edge of the view), and the
    likeliest case named in large type next to it - no label when nothing on
    the list matches
  * markers seen, the lid's measured size and colour, warnings, and the best
    matches among every lid (physical outline) in every thumbnail colour

For aiming the camera, placing the markers and checking the measurements, with
no mill or ShipHero order needed. It holds the probe's serial port, so close
bCNC (or its BLTouch port) first.

    python tools/lidcam_viewer.py                        # auto-finds the board
    python tools/lidcam_viewer.py --port /dev/ttyACM1 --out /tmp/lidcam

Save Photo writes the current photo to --out. Save Empty Bed keeps the current
view as the empty-bed reference (same files as lidcam_snapshot.py uses), after
which the lid is found as whatever differs from it.

Set Lid Corner records where lids sit: pushed into the fixture's corner, every
lid's upper-left corner in one place. Place a lid that outlines well (a light
colour) in the corner and press it; afterwards a lid in the corner is anchored
there, and only its far side and far end are measured. Saved in --out.

Calibrate Exposure fixes the camera's exposure for every lid (bCNC/LidExposure):
place the brightest lid (Aluminum), measure, swap in the darkest (Matte Black)
at the same spot, measure, save. It is saved per machine, where bCNC reads it
too (~/.bCNC-lidcam-exposure.json), and applied whenever the camera connects.
Needs camera firmware 2 (firmware/bltouch_camera).
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bCNC"))

import cv2 as cv  # noqa: E402
import serial  # noqa: E402
from PIL import Image, ImageDraw, ImageFont, ImageTk  # noqa: E402

import BltCamera  # noqa: E402
import LidExposure  # noqa: E402
import LidVision  # noqa: E402
import lidcam_snapshot as snap  # noqa: E402


class Worker(threading.Thread):
    """Takes photos and runs the vision on each, off the Tk thread."""

    def __init__(self, port_name, marker_mm, out_dir, results):
        super().__init__(daemon=True)
        self.port_name = port_name
        self.marker_mm = marker_mm
        self.out_dir = out_dir
        self.results = results
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.reload_background = threading.Event()
        self.candidates = snap.candidates()
        self._refs = {}
        self.background = None
        self.corner = None              # LidCorner, or None
        self.reload_background.set()
        # Work that needs the port to itself (exposure calibration), run
        # between photos: callables taking the port.
        self.jobs = queue.Queue()
        self.version = 0
        self.exposure_path = BltCamera.default_exposure_path()
        self.exposure = None        # the fixed exposure in use, or None: automatic

    def ref(self, key):
        if key not in self._refs:
            self._refs[key] = LidVision.colour_of_thumbnail(os.path.join(snap.THUMBS, key + ".png"))
        return self._refs[key]

    def _load_background(self):
        """The empty-bed map and the fixture corner, as saved in out_dir."""
        self.corner = load_corner(self.out_dir)
        self.background = load_bed(self.out_dir)

    def _open(self):
        port = serial.Serial(self.port_name, 9600, timeout=0.05)
        time.sleep(0.3)
        port.reset_input_buffer()
        self.version, has_camera = BltCamera.identify_version(port, 2.0)
        if not has_camera:
            port.close()
            raise BltCamera.CameraError("the board did not answer as camera firmware")
        self.exposure = BltCamera.apply_saved_exposure(port, self.version, self.exposure_path)
        return port

    def _run_job(self, port):
        """Run one queued job with the port, if any. Its result (or error)
        goes to the results queue as ("job", name, value, error)."""
        try:
            name, fn = self.jobs.get_nowait()
        except queue.Empty:
            return False
        try:
            self.results.put(("job", name, fn(port), None))
        except Exception as e:          # reported to the dialog, never raised
            self.results.put(("job", name, None, e))
        return True

    def run(self):
        port = None
        first = True
        while not self.stopped.is_set():
            if self.paused.is_set() and self.jobs.empty():
                time.sleep(0.1)
                continue
            try:
                if port is None:
                    port = self._open()
                    first = True
                if self.reload_background.is_set():
                    self.reload_background.clear()
                    self._load_background()
                if self._run_job(port):
                    continue
                if self.paused.is_set():
                    continue
                port.reset_input_buffer()
                t0 = time.time()
                shot = BltCamera.capture(port, first=first)
                first = False
                t1 = time.time()
                res = LidVision.identify(shot.jpeg, self.candidates, self.ref, self.marker_mm,
                                         background=self.background, corner=self.corner)
                t2 = time.time()
                self.results.put(("frame", shot, res, t1 - t0, t2 - t1))
            except BltCamera.CameraError as e:
                self.results.put(("error", str(e)))
                time.sleep(0.5)
            except (OSError, serial.SerialException) as e:
                self.results.put(("error", "port: %s" % e))
                try:
                    if port is not None:
                        port.close()
                except Exception:
                    pass
                port = None
                time.sleep(1.0)
        if port is not None:
            port.close()


class Viewer:
    def __init__(self, root, worker, out_dir, marker_mm, font_size):
        self.root = root
        self._setup_fonts(font_size)
        self.worker = worker
        self.out_dir = out_dir
        self.marker_mm = marker_mm
        self.results = worker.results
        self.last = None            # (shot, res) on show
        self.frames = 0
        self.times = []

        root.title("Lid Camera - live view")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("%dx%d" % (int(sw * 0.9), int(sh * 0.85)))
        root.configure(bg="#202020")

        bar = tk.Frame(root, bg="#202020")
        bar.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)
        self.pause_btn = ttk.Button(bar, text="Pause", command=self.toggle_pause)
        self.pause_btn.pack(side=tk.LEFT)
        ttk.Button(bar, text="Save Photo", command=self.save_photo).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Save Empty Bed (no lid)", command=self.save_empty_bed).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Clear Empty Bed", command=self.clear_empty_bed).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Set Lid Corner", command=self.set_lid_corner).pack(side=tk.LEFT, padx=(16, 4))
        ttk.Button(bar, text="Calibrate Exposure", command=self.calibrate_exposure).pack(side=tk.LEFT, padx=(16, 4))
        self.status = tk.StringVar(value="Connecting to %s..." % worker.port_name)
        tk.Label(bar, textvariable=self.status, fg="#e0e0e0", bg="#202020",
                 font=self.f_text).pack(side=tk.LEFT, padx=12)
        tk.Label(bar, text="Ctrl + / Ctrl - : text size", fg="#8d8d8d", bg="#202020",
                 font=self.f_small).pack(side=tk.RIGHT)

        body = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashwidth=6, bg="#202020")
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.cam = self._panel(body, "Camera")
        info = tk.Frame(body, bg="#2a2a2a")
        self.info = tk.Text(info, width=40, bg="#2a2a2a", fg="#e8e8e8", bd=0,
                            highlightthickness=0, font=self.f_mono, wrap=tk.WORD,
                            spacing1=2)
        self.info.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.info.tag_configure("head", font=self.f_head, foreground="#ffffff", spacing1=10)
        self.info.tag_configure("good", foreground="#6fdc8c")
        self.info.tag_configure("mid", foreground="#f1c21b")
        self.info.tag_configure("bad", foreground="#ff8389")
        self.info.tag_configure("dim", foreground="#a0a0a0")
        win_w = int(sw * 0.9)
        body.add(self.cam[0], stretch="always", minsize=300, width=int(win_w * 0.72))
        body.add(info, minsize=300, width=int(win_w * 0.28))

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<space>", lambda e: self.toggle_pause())
        for seq in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            root.bind(seq, lambda e: self.zoom(+1))
        for seq in ("<Control-minus>", "<Control-KP_Subtract>"):
            root.bind(seq, lambda e: self.zoom(-1))
        root.bind("<Control-0>", lambda e: self.zoom(0))
        root.after(50, self.poll)

    # -------------------------------------------------------------- fonts
    def _setup_fonts(self, size):
        """Every widget uses these named fonts, so zooming resizes them all.

        Sizes are in points, which Tk scales by the display's DPI. (A Tk built
        without Xft cannot scale fonts at all and shows tiny bitmap text; the
        conda-forge tk needs its xft build.)
        """
        self.base_size = self.font_size = size or 11
        sans = tkfont.nametofont("TkDefaultFont").actual("family")
        mono = tkfont.nametofont("TkFixedFont").actual("family")
        self.f_text = tkfont.Font(family=sans)
        self.f_small = tkfont.Font(family=sans)
        self.f_head = tkfont.Font(family=sans, weight="bold")
        self.f_mono = tkfont.Font(family=mono)
        self.zoom(0)

    def zoom(self, step):
        size = self.base_size if step == 0 else max(6, min(30, self.font_size + step))
        self.font_size = size
        for f, k in ((self.f_text, 1.0), (self.f_small, 0.85), (self.f_head, 1.15), (self.f_mono, 1.0)):
            f.configure(size=max(6, round(size * k)))
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(size=size)
        ttk.Style().configure("TButton", font=self.f_text, padding=(4, 2))

    def _panel(self, parent, title):
        f = tk.Frame(parent, bg="#2a2a2a")
        # The pane's size comes from the window, never from the image in it:
        # otherwise each new image asks for the last one's size and the panes
        # creep until one swallows the others.
        f.pack_propagate(False)
        tk.Label(f, text=title, fg="#c6c6c6", bg="#2a2a2a", anchor="w",
                 font=self.f_text).pack(side=tk.TOP, fill=tk.X, padx=6, pady=(4, 0))
        lbl = tk.Label(f, bg="#111111", font=self.f_text)
        lbl.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=6)
        return f, lbl

    # ------------------------------------------------------------- images
    @staticmethod
    def _show(label, bgr, placeholder, overlay=None):
        """Show `bgr` fitted to `label`. `overlay(image, scale)` then draws on
        the fitted PIL image, so what it draws keeps its size on screen."""
        w, h = max(label.winfo_width(), 50), max(label.winfo_height(), 50)
        if bgr is None:
            label.config(image="", text=placeholder, fg="#a0a0a0", compound=tk.CENTER)
            label.image = None
            return
        ih, iw = bgr.shape[:2]
        scale = min(w / iw, h / ih)
        small = cv.resize(bgr, (max(1, int(iw * scale)), max(1, int(ih * scale))),
                          interpolation=cv.INTER_AREA)
        image = Image.fromarray(cv.cvtColor(small, cv.COLOR_BGR2RGB))
        if overlay is not None:
            overlay(image, scale)
        photo = ImageTk.PhotoImage(image)
        label.config(image=photo, text="")
        label.image = photo

    # ------------------------------------------------------------ the tag
    _fonts = {}

    @classmethod
    def _tag_font(cls, px, bold=True):
        """A system sans font at `px` pixels (Noto Sans here, via fontconfig)."""
        key = (px, bold)
        if key not in cls._fonts:
            font = None
            try:
                import subprocess
                path = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold" if bold else "sans"],
                                      capture_output=True, text=True, timeout=2).stdout.strip()
                font = ImageFont.truetype(path, px)
                if bold:
                    try:
                        font.set_variation_by_name("Bold")     # a variable font
                    except Exception:
                        pass
            except Exception:
                font = ImageFont.load_default(size=px)
            cls._fonts[key] = font
        return cls._fonts[key]

    def _tag_overlay(self, res):
        """Draw the likeliest case's name on the lid, big, in the camera view."""
        best = res.best

        def draw(image, scale):
            d = ImageDraw.Draw(image, "RGBA")
            W, H = image.size
            big = self._tag_font(max(22, int(W / 22)))
            small = self._tag_font(max(16, int(W / 34)), bold=False)
            # Only a real match gets a label: no lid, or a lid that is likelier
            # none of the candidates than any one of them, shows nothing.
            if res.lid_outline is None or best is None or best.probability <= res.none_probability:
                return
            p = best.probability
            colour = (111, 220, 140) if p >= 0.8 else (241, 194, 27) if p >= 0.5 else (255, 131, 137)
            name = best.first["lid"]
            detail = "%s · %d%%" % (best.first["colour_key"].replace("_", " ").title(), round(p * 100))
            if res.lid.length_is_min:
                detail += " · runs off view"
            outline = res.lid_outline * scale

            # The tag goes beside the outline, never over the lid if it can be
            # helped: full size on one line, then the name on two lines, then
            # smaller type, until one fits above, below, right or left of it.
            choice = None
            for k in (1.0, 0.8, 0.65):
                big = self._tag_font(max(18, int(W / 22 * k)))
                small = self._tag_font(max(14, int(W / 34 * k)), bold=False)
                for name_lines in ([name], self._split_name(name)):
                    widths = [d.textlength(t, font=big) for t in name_lines]
                    box_w = max(widths + [d.textlength(detail, font=small)]) + 36
                    box_h = big.size * len(name_lines) + small.size + 40
                    spot = self._place_tag(outline, box_w, box_h, W, H, inside_ok=False)
                    if spot:
                        choice = (big, small, name_lines, box_w, box_h, spot)
                        break
                if choice:
                    break
            if choice is None:                  # nowhere outside: inside, smallest
                spot = self._place_tag(outline, box_w, box_h, W, H, inside_ok=True)
                choice = (big, small, name_lines, box_w, box_h, spot)
            big, small, name_lines, box_w, box_h, (x0, y0) = choice

            d.rounded_rectangle((x0, y0, x0 + box_w, y0 + box_h), radius=14,
                                fill=(0, 0, 0, 185), outline=colour + (255,), width=4)
            y = y0 + 14
            for t in name_lines:
                d.text((x0 + box_w / 2, y), t, font=big, fill=colour, anchor="mt")
                y += big.size
            d.text((x0 + box_w / 2, y + 8), detail, font=small, fill=(235, 235, 235), anchor="mt")

        return draw

    @staticmethod
    def _split_name(name):
        """'Weekly AM-PM Vitamin XL Case' -> two lines of about equal length."""
        words = name.split()
        if len(words) < 2:
            return [name]
        best = min(range(1, len(words)),
                   key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
        return [" ".join(words[:best]), " ".join(words[best:])]

    @staticmethod
    def _place_tag(outline, box_w, box_h, W, H, gap=12, inside_ok=True):
        """Top-left corner for the tag, just outside the outline so it labels
        the lid without covering it: above it if there is room, else below,
        right or left, slid along to stay inside the picture. None if it fits
        nowhere outside and `inside_ok` is False; else inside the top."""
        left, top = outline.min(axis=0).tolist()
        right, bottom = outline.max(axis=0).tolist()
        cx, cy = (left + right) / 2, (top + bottom) / 2
        if box_w > W - 12 or box_h > H - 12:
            return None if not inside_ok else (6, 6)

        def clamp_x(x):
            return min(max(x, 6), W - box_w - 6)

        def clamp_y(y):
            return min(max(y, 6), H - box_h - 6)

        if top - gap - box_h >= 6:                          # above
            return clamp_x(cx - box_w / 2), top - gap - box_h
        if bottom + gap + box_h <= H - 6:                   # below
            return clamp_x(cx - box_w / 2), bottom + gap
        if right + gap + box_w <= W - 6:                    # right
            return right + gap, clamp_y(cy - box_h / 2)
        if left - gap - box_w >= 6:                         # left
            return left - gap - box_w, clamp_y(cy - box_h / 2)
        if not inside_ok:
            return None
        return clamp_x(cx - box_w / 2), clamp_y(top + gap)

    # --------------------------------------------------------------- text
    def _write_info(self, shot, res, t_cap, t_vis):
        t = self.info
        t.delete("1.0", tk.END)
        markers = sorted(res.plane.layout) if res.plane else []
        t.insert(tk.END, "Markers\n", "head")
        if markers:
            t.insert(tk.END, "  seen: %s\n" % ", ".join(map(str, markers)), "good")
            t.insert(tk.END, "  agree within %.2f mm\n" % res.plane.rms_mm,
                     "good" if res.plane.rms_mm < 1.0 else "mid")
        else:
            t.insert(tk.END, "  none in view\n", "bad")
        t.insert(tk.END, "\nLid\n", "head")
        if res.lid is not None:
            t.insert(tk.END, "  %s\n" % LidVision.describe_size(res.lid))
            if res.lid.length_is_min:
                t.insert(tk.END, "  runs off the view: length is a minimum\n", "mid")
            t.insert(tk.END, "  found by %s\n" % ("empty-bed difference" if res.lid.method == "background"
                                                   else "edges"), "dim")
        else:
            t.insert(tk.END, "  not found\n", "bad")
        if res.colour_lab is not None:
            t.insert(tk.END, "  colour  L %.0f  a %.0f  b %.0f\n" % tuple(res.colour_lab), "dim")
        if res.warnings:
            t.insert(tk.END, "\nWarnings\n", "head")
            for w in res.warnings:
                t.insert(tk.END, "  " + w + "\n", "mid")
        t.insert(tk.END, "\nBest matches\n", "head")
        matches = res.matches
        if res.lid is None:
            # No size: every lid in a colour scores the same, so list colours once.
            t.insert(tk.END, "  (colour only - no size measured)\n", "dim")
            seen, matches = set(), []
            for m in res.matches:
                if m.first["colour_key"] not in seen:
                    seen.add(m.first["colour_key"])
                    matches.append(m)
        for m in matches[:6]:
            p = m.probability
            tag = "good" if p >= 0.8 else "mid" if p >= 0.5 else "dim"
            t.insert(tk.END, "  %5.1f%%  %s\n" % (p * 100, m.first["lid"]), tag)
            t.insert(tk.END, "          %s  (size %s, colour %s)\n" % (
                m.first["colour_key"].replace("_", " "),
                "-" if m.size_error is None else "%.1fσ" % m.size_error,
                "-" if m.colour_error is None else "%.1fσ" % m.colour_error), "dim")
        t.insert(tk.END, "  %5.1f%%  none of these\n" % (res.none_probability * 100), "dim")
        bg = "on" if self.worker.background is not None else "off"
        t.insert(tk.END, "\nPhoto %dx%d, %d kB · capture %.2fs · vision %.2fs · empty bed %s\n"
                 % (shot.width, shot.height, len(shot.jpeg) // 1024, t_cap, t_vis, bg), "dim")
        t.insert(tk.END, "Exposure: %s\n" % exposure_text(self.worker), "dim")

    # --------------------------------------------------------------- loop
    def poll(self):
        latest = None
        try:
            while True:
                item = self.results.get_nowait()
                if item[0] in ("job", "log"):
                    self._job_item(item)
                    continue
                latest = item
                if latest[0] == "error":
                    self.status.set("Camera: %s" % latest[1])
        except queue.Empty:
            pass
        if latest is not None and latest[0] == "frame":
            _kind, shot, res, t_cap, t_vis = latest
            self.last = (shot, res)
            self.frames += 1
            now = time.time()
            self.times = [x for x in self.times if now - x < 5] + [now]
            fps = (len(self.times) - 1) / max(self.times[-1] - self.times[0], 1e-6) if len(self.times) > 1 else 0
            self.status.set("%s · %d photos · %.1f per second%s" % (
                self.worker.port_name, self.frames, fps,
                " · PAUSED" if self.worker.paused.is_set() else ""))
            self._show(self.cam[1], res.annotated, "", self._tag_overlay(res))
            self._write_info(shot, res, t_cap, t_vis)
        self.root.after(50, self.poll)

    # ------------------------------------------------------------ buttons
    def toggle_pause(self):
        if self.worker.paused.is_set():
            self.worker.paused.clear()
            self.pause_btn.config(text="Pause")
        else:
            self.worker.paused.set()
            self.pause_btn.config(text="Resume")
            self.status.set(self.status.get().split(" · PAUSED")[0] + " · PAUSED")

    def save_photo(self):
        if not self.last:
            return
        name = time.strftime("photo-%Y%m%d-%H%M%S")
        open(os.path.join(self.out_dir, name + ".jpg"), "wb").write(self.last[0].jpeg)
        cv.imwrite(os.path.join(self.out_dir, name + "-annotated.jpg"), self.last[1].annotated)
        self.status.set("Saved %s" % os.path.join(self.out_dir, name + ".jpg"))

    def save_empty_bed(self):
        """Keep the current view as the empty bed. True when saved."""
        if not self.last:
            return False
        # A lid left on the bed becomes part of "empty", and every lid after is
        # then measured against it. Look for one first (by its edges: the
        # empty-bed comparison cannot see a lid that is in both photos). A dark
        # lid on the black fixture can still slip through, hence the reminder.
        # The photo is analysed afresh, without the old empty bed: the live
        # view's result is framed by the old empty bed's area, and a new one
        # saved from it would inherit that framing even after the camera moved.
        fresh = LidVision.identify(self.last[0].jpeg, snap.candidates(), lambda key: None,
                                   self.marker_mm)
        found = fresh.lid
        question = ("Save the empty-bed photo?\n\nIt must show the bed with NO lid on it: "
                    "every lid afterwards is found by how it differs from this photo.")
        if found is not None:
            question = ("There seems to be a lid on the bed (%s).\n\nAn empty-bed photo must "
                        "show the bed with no lid on it. Save it anyway?"
                        % LidVision.describe_size(found))
        if not messagebox.askyesno("Save Empty Bed", question, parent=self.root,
                                   icon="warning" if found is not None else "question"):
            return False
        if fresh.plane is None:
            self.status.set("Empty bed not saved: no markers in view.")
            return False
        # Added to the map: this view replaces what the map had for the ground
        # it shows, and the rest of the map stays.
        old = self.worker.background
        if old is not None and old.ref == fresh.plane.ref and old.marker_mm == self.marker_mm:
            bed = old.copy()
            bed.add(fresh.rectified, fresh.valid, fresh.region, fresh.ppm, replace=True)
        else:
            bed = LidVision.BedMap.from_result(fresh, self.marker_mm)
        bed.save(bed_base(self.out_dir))
        self.worker.background = bed            # swapped whole: the worker never sees half
        self.status.set("Empty bed saved to the map: the lid is whatever differs from it.")
        return True

    def learn_bed(self, res):
        """A confirmed lid: the bare fixture around it updates the map."""
        old = self.worker.background
        if old is None or res is None:
            return False
        bed = old.copy()
        if not bed.learn(res):
            return False
        bed.save(bed_base(self.out_dir))
        self.worker.background = bed
        return True

    def set_lid_corner(self):
        """Record the fixture's corner from the lid on the bed. True when saved."""
        if not self.last:
            return False
        # Found afresh, not anchored on the corner already saved.
        res = LidVision.identify(self.last[0].jpeg, snap.candidates(), lambda key: None,
                                 self.marker_mm, background=self.worker.background)
        corner = LidVision.corner_from_result(res)
        if corner is None or res.lid.length_is_min:
            messagebox.showinfo("Set Lid Corner", "Place a lid in the fixture's corner, fully "
                                "in view, so its outline is found all round. A light-coloured "
                                "lid works best.", parent=self.root)
            return False
        if not messagebox.askyesno(
                "Set Lid Corner",
                "Use this lid (%s) to set where lids sit?\n\nIt must be pushed into the "
                "fixture's corner the way every lid is, and its green outline must follow the "
                "lid exactly, at the corner most of all. Lids in that corner are then anchored "
                "on it." % LidVision.describe_size(res.lid), parent=self.root):
            return False
        with open(corner_path(self.out_dir), "w") as f:
            json.dump(corner.to_dict(), f, indent=1)
        self.worker.reload_background.set()
        self.status.set("Lid corner saved: lids in the fixture's corner are anchored on it.")
        return True

    def clear_empty_bed(self):
        base = bed_base(self.out_dir)
        for path in (base + ".png", base + "-count.png", base + ".json",
                     os.path.join(self.out_dir, "empty-bed.png"),
                     os.path.join(self.out_dir, "empty-bed.json")):
            try:
                os.remove(path)
            except OSError:
                pass
        self.worker.reload_background.set()
        self.status.set("Empty bed cleared: the lid is found by its edges.")

    def close(self):
        self.worker.stopped.set()
        self.root.after(300, self.root.destroy)

    # ------------------------------------------------------- calibration
    def calibrate_exposure(self):
        if getattr(self, "calibration", None) is not None:
            self.calibration.top.lift()
            return
        if self.worker.version and self.worker.version < BltCamera.EXPOSURE_VERSION:
            messagebox.showinfo("Calibrate Exposure", "The camera firmware cannot fix its "
                                "exposure. Flash firmware/bltouch_camera (version 2) first.",
                                parent=self.root)
            return
        self.calibration = ExposureCalibration(self)

    def _job_item(self, item):
        cal = getattr(self, "calibration", None)
        if cal is not None:
            cal.handle(item)


def bed_base(out_dir):
    """Where the empty-bed map is kept: <out>/empty-bed-map.png, -count.png, .json."""
    return os.path.join(out_dir, "empty-bed-map")


def load_bed(out_dir):
    """The empty-bed map, or one made from an older single empty-bed photo."""
    return LidVision.load_bed(bed_base(out_dir), os.path.join(out_dir, "empty-bed"))


def corner_path(out_dir):
    return os.path.join(out_dir, "lid-corner.json")


def load_corner(out_dir):
    """The saved fixture corner (LidVision.LidCorner), or None."""
    try:
        with open(corner_path(out_dir)) as f:
            return LidVision.LidCorner.from_dict(json.load(f))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def exposure_text(worker):
    rec = worker.exposure
    if rec is None:
        return "automatic" if worker.version >= BltCamera.EXPOSURE_VERSION \
            else "automatic (firmware cannot fix it)"
    return "fixed, exposure %s, gain %s, %s white balance" % (
        rec["aec"], rec["agc"], BltCamera.WB_PRESETS.get(rec["wb"], rec["wb"]))


class ExposureCalibration:
    """The two-lid exposure calibration, as a small window over the viewer.
    The measuring runs on the camera worker (it owns the port); this only
    asks for it and shows what comes back."""

    def __init__(self, app):
        self.app = app
        self.worker = app.worker
        self.was_paused = self.worker.paused.is_set()
        self.worker.paused.set()            # no live photos in between
        self.settings = self.area = self.bright = self.dark = self.verdict = None
        self.saved_ok = False
        top = self.top = tk.Toplevel(app.root)
        top.title("Calibrate Exposure")
        top.configure(bg="#2a2a2a")
        top.transient(app.root)
        top.protocol("WM_DELETE_WINDOW", self.cancel)
        self.prompt = tk.StringVar()
        prompt = tk.Label(top, textvariable=self.prompt, fg="#ffffff", bg="#2a2a2a", font=app.f_head,
                          justify=tk.LEFT, anchor="w", wraplength=560)
        prompt.pack(fill=tk.X, padx=12, pady=(12, 6))
        # Wrap at the window's width, whatever the text size.
        prompt.bind("<Configure>", lambda e: prompt.config(wraplength=max(200, e.width - 8)))
        self.log = tk.Text(top, width=72, height=16, bg="#1e1e1e", fg="#e8e8e8", bd=0,
                           highlightthickness=0, font=app.f_mono, wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True, padx=12)
        self.log.tag_configure("good", foreground="#6fdc8c")
        self.log.tag_configure("bad", foreground="#ff8389")
        buttons = tk.Frame(top, bg="#2a2a2a")
        buttons.pack(fill=tk.X, padx=12, pady=12)
        self.go = ttk.Button(buttons, command=self.step)
        self.go.pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side=tk.RIGHT, padx=8)
        self.stage = "bright"
        self._show_stage()

    def _show_stage(self):
        texts = {
            "bright": ("Place the BRIGHTEST lid (Aluminum) where lids go, fully in view, "
                       "then press Measure. About 15 seconds.", "Measure"),
            "dark": ("Now swap in the DARKEST lid (Matte Black) at the same spot, the same "
                     "way round, then press Measure.", "Measure"),
            "done": ("Save these settings? The camera will use them for every photo, here "
                     "and in bCNC.", "Save"),
            "busy": ("Measuring... keep the lid still.", "Measure"),
        }
        prompt, button = texts[self.stage]
        self.prompt.set(prompt)
        self.go.config(text=button)
        self.go.state(["disabled"] if self.stage == "busy" else ["!disabled"])

    def write(self, text, tag=None):
        self.log.insert(tk.END, text + "\n", tag)
        self.log.see(tk.END)

    def _take(self, port):
        """take(aec, agc, wb) -> JPEG, on the worker's thread."""
        def take(aec, agc, wb):
            BltCamera.set_exposure(port, aec, agc, wb)
            port.reset_input_buffer()
            return BltCamera.capture(port, first=True).jpeg
        return take

    def _log_to_queue(self, text):
        self.worker.results.put(("log", text))

    def step(self):
        stage = self.stage
        if stage == "bright":
            self.stage = "busy"
            self._show_stage()
            cands = snap.candidates()
            mm = self.app.marker_mm
            self.worker.jobs.put(("bright", lambda port: LidExposure.calibrate_bright(
                self._take(port), cands, mm, None, log=self._log_to_queue)))
        elif stage == "dark":
            self.stage = "busy"
            self._show_stage()
            settings, area, bright = self.settings, self.area, self.bright
            self.worker.jobs.put(("dark", lambda port: LidExposure.check_dark(
                self._take(port), settings, area, bright, log=self._log_to_queue)))
        elif stage == "done":
            rec = LidExposure.record(self.settings, self.bright, self.dark, self.verdict)
            BltCamera.save_exposure(self.worker.exposure_path, rec)

            def apply(port, rec=rec):
                BltCamera.set_exposure(port, rec["aec"], rec["agc"], rec["wb"])
                self.worker.exposure = rec
            self.worker.jobs.put(("apply", apply))
            self.saved_ok = True
            self.app.status.set("Exposure saved: %s" % self.worker.exposure_path)
            messagebox.showinfo(
                "Exposure saved",
                "Saved to %s.\n\nIf an empty-bed photo is in use, save it again: it was "
                "taken with the old exposure, and the lid is found by comparing with it."
                % self.worker.exposure_path, parent=self.top)
            self._close()

    def handle(self, item):
        if item[0] == "log":
            self.write(item[1])
            return
        _kind, name, value, error = item
        if name == "restore":
            return
        if error is not None:
            self.write("Stopped: %s" % error, "bad")
            self.stage = "bright" if name == "bright" else "dark" if name == "dark" else self.stage
            self._show_stage()
            return
        if name == "bright":
            self.settings, self.area, self.bright = value
            self.write("Bright lid: exposure %d, gain 0, %s white balance. Its brightest %.0f "
                       "of 255, marker borders %s." % (
                           self.settings["aec"], BltCamera.WB_PRESETS[self.settings["wb"]],
                           self.bright["lid_hi"],
                           "not seen" if self.bright["border_hi"] is None
                           else "%.0f" % self.bright["border_hi"]), "good")
            self.stage = "dark"
        elif name == "dark":
            self.dark, self.verdict, ok = value
            self.write(self.verdict, "good" if ok else "bad")
            self.stage = "done" if ok else "dark"
        self._show_stage()

    def cancel(self):
        if not self.saved_ok:
            prev = BltCamera.load_exposure(self.worker.exposure_path)

            def restore(port, prev=prev):
                if prev:
                    BltCamera.set_exposure(port, prev["aec"], prev["agc"], prev["wb"])
                elif self.worker.version >= BltCamera.EXPOSURE_VERSION:
                    BltCamera.set_auto_exposure(port)
                self.worker.exposure = prev
            self.worker.jobs.put(("restore", restore))
        self._close()

    def _close(self):
        if not self.was_paused:
            self.worker.paused.clear()
        self.app.calibration = None
        self.top.destroy()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", help="the XIAO's serial port (default: auto)")
    ap.add_argument("--out", default=os.path.expanduser("~/lidcam"),
                    help="folder for saved photos and the empty-bed reference")
    ap.add_argument("--marker-mm", type=float, default=LidVision.DEFAULT_MARKER_MM)
    ap.add_argument("--font-size", type=int, default=11, help="text size in points (default 11)")
    ap.add_argument("--exit-after", type=float, help=argparse.SUPPRESS)   # for testing
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    results = queue.Queue()
    worker = Worker(a.port or snap.find_port(), a.marker_mm, a.out, results)
    root = tk.Tk()
    viewer = Viewer(root, worker, a.out, a.marker_mm, a.font_size)
    worker.start()
    if a.exit_after:
        root.after(int(a.exit_after * 1000), lambda: (print("photos shown:", viewer.frames),
                                                      viewer.close()))
    root.mainloop()


if __name__ == "__main__":
    main()
