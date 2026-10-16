#!/usr/bin/env python3
"""Live view of the lid camera, with what the vision makes of each photo.

A window showing, refreshed as fast as the camera delivers (about two photos a
second):

  * the camera's photo, with the markers it found (orange) and the lid outline
    (green) drawn on it
  * the top-down view in millimetres, white-balanced, that the lid is
    measured in (once a marker is in view)
  * markers seen, the lid's measured size and colour, warnings, and the best
    matches among every configured lid in every thumbnail colour

For aiming the camera, placing the markers and checking the measurements, with
no mill or ShipHero order needed. It holds the probe's serial port, so close
bCNC (or its BLTouch port) first.

    python tools/lidcam_viewer.py                        # auto-finds the board
    python tools/lidcam_viewer.py --port /dev/ttyACM1 --out /tmp/lidcam

Save Photo writes the current photo to --out. Save Empty Bed keeps the current
view as the empty-bed reference (same files as lidcam_snapshot.py uses), after
which the lid is found as whatever differs from it.
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
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bCNC"))

import cv2 as cv  # noqa: E402
import serial  # noqa: E402
from PIL import Image, ImageTk  # noqa: E402

import BltCamera  # noqa: E402
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
        self.reload_background.set()

    def ref(self, key):
        if key not in self._refs:
            self._refs[key] = LidVision.colour_of_thumbnail(os.path.join(snap.THUMBS, key + ".png"))
        return self._refs[key]

    def _load_background(self):
        base = os.path.join(self.out_dir, "empty-bed")
        self.background = None
        if os.path.exists(base + ".json"):
            bg = json.load(open(base + ".json"))
            bg["image"] = cv.imread(base + ".png")
            if bg["image"] is not None:
                self.background = bg

    def _open(self):
        port = serial.Serial(self.port_name, 9600, timeout=0.05)
        time.sleep(0.3)
        port.reset_input_buffer()
        if not BltCamera.identify(port, 2.0):
            port.close()
            raise BltCamera.CameraError("the board did not answer as camera firmware")
        return port

    def run(self):
        port = None
        first = True
        while not self.stopped.is_set():
            if self.paused.is_set():
                time.sleep(0.1)
                continue
            try:
                if port is None:
                    port = self._open()
                    first = True
                if self.reload_background.is_set():
                    self.reload_background.clear()
                    self._load_background()
                port.reset_input_buffer()
                t0 = time.time()
                shot = BltCamera.capture(port, first=first)
                first = False
                t1 = time.time()
                res = LidVision.identify(shot.jpeg, self.candidates, self.ref, self.marker_mm,
                                         background=self.background)
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
        ttk.Button(bar, text="Save Empty Bed", command=self.save_empty_bed).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Clear Empty Bed", command=self.clear_empty_bed).pack(side=tk.LEFT, padx=4)
        self.status = tk.StringVar(value="Connecting to %s..." % worker.port_name)
        tk.Label(bar, textvariable=self.status, fg="#e0e0e0", bg="#202020",
                 font=self.f_text).pack(side=tk.LEFT, padx=12)
        tk.Label(bar, text="Ctrl + / Ctrl - : text size", fg="#8d8d8d", bg="#202020",
                 font=self.f_small).pack(side=tk.RIGHT)

        body = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashwidth=6, bg="#202020")
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.cam = self._panel(body, "Camera")
        self.top = self._panel(body, "Top-down (mm, white-balanced)")
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
        body.add(self.cam[0], stretch="always", minsize=300, width=int(win_w * 0.46))
        body.add(self.top[0], stretch="always", minsize=200, width=int(win_w * 0.24))
        body.add(info, minsize=300, width=int(win_w * 0.30))

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
        tk.Label(f, text=title, fg="#c6c6c6", bg="#2a2a2a", anchor="w",
                 font=self.f_text).pack(side=tk.TOP, fill=tk.X, padx=6, pady=(4, 0))
        lbl = tk.Label(f, bg="#111111", font=self.f_text)
        lbl.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=6)
        return f, lbl

    # ------------------------------------------------------------- images
    @staticmethod
    def _show(label, bgr, placeholder):
        w, h = max(label.winfo_width(), 50), max(label.winfo_height(), 50)
        if bgr is None:
            label.config(image="", text=placeholder, fg="#a0a0a0", compound=tk.CENTER)
            label.image = None
            return
        ih, iw = bgr.shape[:2]
        scale = min(w / iw, h / ih)
        small = cv.resize(bgr, (max(1, int(iw * scale)), max(1, int(ih * scale))),
                          interpolation=cv.INTER_AREA)
        photo = ImageTk.PhotoImage(Image.fromarray(cv.cvtColor(small, cv.COLOR_BGR2RGB)))
        label.config(image=photo, text="")
        label.image = photo

    def _topdown(self, res):
        if res.rectified is None:
            return None
        view = res.rectified.copy()
        if res.lid is not None:
            colour = (0, 200, 255) if res.lid.length_is_min else (0, 220, 0)   # as in the photo
            cv.polylines(view, [res.lid.box_px.astype("int32")], True, colour, 2)
        # 10 mm grid ticks along the edges, to read sizes by eye
        x0, y0, x1, y1 = res.region
        ppm = view.shape[1] / (x1 - x0)
        for mm in range(int(x0 // 10 * 10), int(x1), 10):
            x = int((mm - x0) * ppm)
            cv.line(view, (x, 0), (x, 12 if mm % 50 else 24), (255, 255, 255), 1)
        for mm in range(int(y0 // 10 * 10), int(y1), 10):
            y = int((mm - y0) * ppm)
            cv.line(view, (0, y), (12 if mm % 50 else 24, y), (255, 255, 255), 1)
        return view

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

    # --------------------------------------------------------------- loop
    def poll(self):
        latest = None
        try:
            while True:
                latest = self.results.get_nowait()
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
            self._show(self.cam[1], res.annotated, "")
            self._show(self.top[1], self._topdown(res), "No markers in view:\nno top-down view")
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
        if not self.last:
            return
        rec = LidVision.background_record(self.last[1], self.marker_mm)
        if rec is None:
            self.status.set("Empty bed not saved: no markers in view.")
            return
        base = os.path.join(self.out_dir, "empty-bed")
        cv.imwrite(base + ".png", rec["image"])
        json.dump({k: v for k, v in rec.items() if k != "image"}, open(base + ".json", "w"))
        self.worker.reload_background.set()
        self.status.set("Empty bed saved: the lid is now whatever differs from it.")

    def clear_empty_bed(self):
        for ext in (".png", ".json"):
            try:
                os.remove(os.path.join(self.out_dir, "empty-bed" + ext))
            except OSError:
                pass
        self.worker.reload_background.set()
        self.status.set("Empty bed cleared: the lid is found by its edges.")

    def close(self):
        self.worker.stopped.set()
        self.root.after(300, self.root.destroy)


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
