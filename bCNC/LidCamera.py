"""Lid camera for Tote Scanning: photograph the bed, pick the lid, confirm.

After a tote is scanned (or on "Identify Lid"), the camera on the BLTouch board
takes a photo of the work area. LidVision measures the lid's size and colour
and scores them against the tote's remaining lids. The best match is selected
in the list, and a dialog shows the photo, the pick and its confidence. The
operator confirms, which starts the engraving (Engrave Selected, then
Align & Run), or picks another, or retakes the photo.

Nothing here can delay probing:
  * No photo is taken while the machine runs, Align & Run is probing, or Auto
    Setup is working. The check is made before each photo.
  * The capture and the vision run on a worker thread. The Tk thread only
    polls for the result, so the window never freezes either.
  * The capture shares the probe port's lock with the servo commands (see
    Sender.blt_camera_capture and BltCamera).

Per-machine files, next to the bCNC ini:
  <ini>-lidcam-last.jpg          the last photo, for checking what the camera sees
  <ini>-lidcam-background.png/.json   the empty-bed photo, if one was saved
  <ini>-lidcam-colours.json      colours learned from confirmed lids
"""

import json
import os
import threading
import time
from tkinter import (
    BOTH, LEFT, RIGHT, TOP, W, X, YES,
    BooleanVar, Button, Checkbutton, Entry, Frame, Label, LabelFrame, StringVar,
    Toplevel, messagebox,
)

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


def _path(suffix):
    return Utils.iniUser + "-lidcam-" + suffix


def enabled():
    return Utils.getBool(_SECTION, "enabled", True)


def auto_after_scan():
    return Utils.getBool(_SECTION, "autoAfterScan", True)


def marker_mm():
    v = Utils.getFloat(_SECTION, "markerMM", LidVision.DEFAULT_MARKER_MM)
    return v if v > 0 else LidVision.DEFAULT_MARKER_MM


def size_scale():
    v = Utils.getFloat(_SECTION, "sizeScale", 1.0)
    return v if 0.5 < v < 2.0 else 1.0


def load_background():
    """The saved empty-bed record, or None."""
    try:
        import cv2 as cv
        with open(_path("background.json")) as f:
            meta = json.load(f)
        img = cv.imread(_path("background.png"), cv.IMREAD_COLOR)
        if img is None:
            return None
        meta["image"] = img
        return meta
    except (OSError, ValueError, ImportError):
        return None


def save_background(record):
    import cv2 as cv
    cv.imwrite(_path("background.png"), record["image"])
    meta = {k: v for k, v in record.items() if k != "image"}
    with open(_path("background.json"), "w") as f:
        json.dump(meta, f)


def clear_background():
    for name in ("background.png", "background.json"):
        try:
            os.remove(_path(name))
        except OSError:
            pass


class Controller:
    """The camera side of an EngravingFrame (`frame`)."""

    def __init__(self, frame):
        self.frame = frame
        self.app = frame.app
        self.colours = LidVision.ColourMemory(_path("colours.json"))
        self._thumb_colours = {}
        self._job = None            # worker in flight: {"thread", "result", "error", "done"}
        self._dialog = None

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

    # ========================================================= candidates
    def _candidates(self):
        """The tote's lids still to engrave, as LidVision candidates."""
        f = self.frame
        gen = f._gen()
        if not hasattr(gen, "_lid_defaults"):
            gen._lid_defaults = gen._load_lid_defaults()
        out = []
        for i in f._remaining():
            row = f.rows[i]
            lid = f._lid_for(i)
            code = (row.get("colour_code") or "").upper()
            out.append({
                "key": "%s|%s" % (lid or row.get("product_code") or "?", code),
                "size": PillcaseOrder.lid_dimensions(gen._lid_defaults.get(lid)) if lid else None,
                "colour_key": code,
                "row_index": i,
                "lid": lid,
                "row": row,
            })
        return out

    def _reference_colour(self, code):
        """How a colour should look: learned from this camera if it has seen
        it confirmed, else the colour's thumbnail."""
        learned = self.colours.get(code)
        if learned is not None:
            return learned
        if code not in self._thumb_colours:
            path = None
            for row in self.frame.rows:
                if (row.get("colour_code") or "").upper() == code:
                    path = PillcaseOrder.find_thumbnail(
                        PillcaseOrder.thumbnail_dirs(Utils.getStr("SurfAlign", "thumbnailsDir", "")),
                        code, row.get("colour_name"))
                    break
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
                self.frame._warn(_("Scan a tote first - there are no lids left to match."))
            return
        self.frame._status(_("📷 Looking at the bed..."))
        bg = load_background()
        mm, scale = marker_mm(), size_scale()
        # Looked up here, on the Tk thread: the worker touches no config or rows.
        refs = {c["colour_key"]: self._reference_colour(c["colour_key"]) for c in candidates}

        def work():
            shot = self._photo()
            return LidVision.identify(shot.jpeg, candidates, refs.get, mm, scale, bg)

        def failed(e):
            msg = _("Lid camera: %s") % e
            if auto:
                # A scan must never be held up by the camera.
                self.frame._status(msg)
            else:
                self.frame._status(msg, error=True)
                self.frame._warn(msg)

        self._start(work, lambda res: self._show(res, auto), failed)

    # ============================================================== dialog
    def _select_row(self, match):
        """Select the first not-yet-engraved row of a match in the list."""
        f = self.frame
        i = match.first["row_index"]
        iid = str(i)
        if f.tree.exists(iid):
            f.tree.selection_set(iid)
            f.tree.focus(iid)
            f.tree.see(iid)
        return i

    def _show(self, res, auto):
        f = self.frame
        if res.best is None or (auto and res.lid is None):
            # Scanned before the lid went in, most likely. After a scan that is
            # normal, so it is only mentioned; asked for, it is shown.
            f._status(_("Lid camera: no lid found. %s") % " ".join(res.warnings), error=not auto)
            if not auto:
                self._dialog_for(res, None)
            return
        self._select_row(res.best)
        f._status(_("📷 Looks like %s - %d%% sure. Confirm to engrave.")
                  % (self._describe(res.best), round(res.confidence * 100)))
        self._dialog_for(res, res.best)

    @staticmethod
    def _describe(match):
        row = match.first["row"]
        case = row.get("case_type") or row.get("product_code") or _("Unknown case")
        return "%s - %s" % (case, row.get("colour_name") or _("colour?"))

    def _dialog_for(self, res, chosen):
        if self._dialog is not None:
            try:
                self._dialog.destroy()
            except Exception:
                pass
        f = self.frame
        d = self._dialog = Toplevel(f)
        d.title(_("Lid Camera"))
        d.transient(f.winfo_toplevel())
        body = Frame(d, padx=12, pady=10)
        body.pack(fill=BOTH, expand=YES)

        img_lbl = Label(body)
        img_lbl.pack(side=TOP)
        self._set_photo(img_lbl, res.annotated)

        state = {"match": chosen}
        head = StringVar()
        conf = StringVar()
        detail = StringVar()
        head_lbl = Label(body, textvariable=head, font=("", 14, "bold"), anchor=W, justify=LEFT)
        head_lbl.pack(side=TOP, fill=X, pady=(8, 0))
        conf_lbl = Label(body, textvariable=conf, font=("", 12, "bold"), anchor=W)
        conf_lbl.pack(side=TOP, fill=X)
        Label(body, textvariable=detail, anchor=W, justify=LEFT, wraplength=PREVIEW_WIDTH).pack(
            side=TOP, fill=X, pady=(2, 4))
        if res.warnings:
            Label(body, text="\n".join(res.warnings), fg="red", anchor=W, justify=LEFT,
                  wraplength=PREVIEW_WIDTH, font=("", 9)).pack(side=TOP, fill=X)

        def show(match):
            state["match"] = match
            if match is None:
                head.set(_("No lid recognised"))
                conf.set("")
                detail.set(_("Place the lid in the fixture where the camera can see it and "
                             "the markers, then Retake."))
                return
            self._select_row(match)
            head.set(self._describe(match))
            p = match.probability
            conf.set(_("Confidence: %d%%") % round(p * 100))
            conf_lbl.config(fg="#1a7f37" if p >= 0.8 else "#b26b00" if p >= LOW_CONFIDENCE else "red")
            texts = [c["row"].get("engraving") or "" for c in match.candidates]
            lines = [_("Lid: %s") % (match.first["lid"] or _("not mapped")),
                     _("Engraving: %s") % texts[0]]
            if len(texts) > 1:
                lines.append(_("%d lids in this tote look the same; the others say: %s")
                             % (len(texts), ", ".join(texts[1:])))
            if res.lid is not None:
                want = match.first.get("size")
                lines.append(_("Measured %s") % LidVision.describe_size(res.lid)
                             + ((_(", lid is %.1f x %.1f mm") % tuple(sorted(want))) if want else ""))
            if match.notes:
                lines.append("; ".join(match.notes))
            detail.set("\n".join(lines))

        show(chosen)

        # Alternatives: anything else with a real chance, best first.
        others = [m for m in res.matches if m is not chosen][:3]
        if others:
            alt = LabelFrame(body, text=_("Or is it..."), padx=6, pady=4)
            alt.pack(side=TOP, fill=X, pady=(4, 0))
            for m in others:
                Button(alt, text="%s  (%d%%)" % (self._describe(m), round(m.probability * 100)),
                       anchor=W, command=lambda m=m: show(m)).pack(side=TOP, fill=X, pady=1)
            if res.none_probability >= 0.2:
                Label(alt, text=_("%d%% chance it is none of this tote's lids.")
                      % round(res.none_probability * 100), fg="red").pack(side=TOP, anchor=W)

        bar = Frame(body)
        bar.pack(side=TOP, fill=X, pady=(10, 0))

        def confirm():
            match = state["match"]
            if match is None:
                return
            if match.probability < LOW_CONFIDENCE and not messagebox.askyesno(
                    _("Low Confidence"),
                    _("The camera is only %d%% sure this is %s.\n\nEngrave it anyway?")
                    % (round(match.probability * 100), self._describe(match)), parent=d):
                return
            self._learn(res, match)
            d.destroy()
            self._dialog = None
            self._engrave(match)

        def select_only():
            match = state["match"]
            if match is not None:
                self._learn(res, match)
            d.destroy()
            self._dialog = None

        def retake():
            d.destroy()
            self._dialog = None
            self.identify()

        go = Button(bar, text=_("✓ Confirm & Engrave"), command=confirm, bg="#4CAF50", fg="white",
                    activebackground="#45a049", activeforeground="white", font=("", 12, "bold"))
        go.pack(side=LEFT, fill=X, expand=YES, padx=(0, 3))
        if chosen is None:
            go.config(state="disabled")
        Button(bar, text=_("Select Only"), command=select_only).pack(side=LEFT, padx=3)
        Button(bar, text=_("Retake"), command=retake).pack(side=LEFT, padx=3)
        Button(bar, text=_("Cancel"), command=lambda: (d.destroy(), setattr(self, "_dialog", None))).pack(
            side=LEFT, padx=(3, 0))

        d.bind("<Escape>", lambda e: (d.destroy(), setattr(self, "_dialog", None)))
        f._gen()._fit_and_center(d)
        go.focus_set()

    def _learn(self, res, match):
        """The operator vouched for this match: remember the colour as seen."""
        code = match.first.get("colour_key")
        if code and res.colour is not None and res.lid is not None:
            self.colours.learn(code, res.colour)

    def _engrave(self, match):
        """Confirmed: Engrave Selected, then Align & Run."""
        f = self.frame
        self._select_row(match)
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
        mm = StringVar(value="%g" % marker_mm())
        scale = StringVar(value="%.4f" % size_scale())
        msg = StringVar()

        Checkbutton(body, text=_("Use the lid camera"), variable=on).grid(
            row=0, column=0, columnspan=2, sticky=W)
        Checkbutton(body, text=_("Identify the lid as soon as a tote is scanned"),
                    variable=auto).grid(row=1, column=0, columnspan=2, sticky=W)
        Label(body, text=_("Marker size (mm):")).grid(row=2, column=0, sticky="e", padx=(0, 5), pady=4)
        Entry(body, textvariable=mm, width=8).grid(row=2, column=1, sticky=W)
        Label(body, text=_("The black square only, not its white border, as printed."),
              font=("", 8, "italic"), fg="gray").grid(row=3, column=1, sticky=W)
        Label(body, text=_("Size correction:")).grid(row=4, column=0, sticky="e", padx=(0, 5), pady=4)
        Entry(body, textvariable=scale, width=8).grid(row=4, column=1, sticky=W)
        Label(body, text=_("Set by 'Calibrate Size' below; 1 means none."),
              font=("", 8, "italic"), fg="gray").grid(row=5, column=1, sticky=W)

        tools = LabelFrame(body, text=_("Calibration"), padx=6, pady=6)
        tools.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        has_bg = load_background() is not None
        bg_state = StringVar(value=_("Empty-bed photo: saved") if has_bg else _("Empty-bed photo: none"))
        Label(tools, textvariable=bg_state, anchor=W).pack(side=TOP, fill=X)

        def busy_msg():
            r = self.why_not()
            if r:
                msg.set(r)
            return r

        def take_background():
            if busy_msg():
                return
            if not messagebox.askokcancel(
                    _("Empty-Bed Photo"),
                    _("Take the lid out of the fixture, leave the markers where they are, "
                      "and press OK."), parent=d):
                return
            msg.set(_("Taking the empty-bed photo..."))
            mm_v = marker_mm()

            def work():
                shot = self._photo()
                res = LidVision.identify(shot.jpeg, [], lambda k: None, mm_v)
                return LidVision.background_record(res, mm_v)

            def done(rec):
                if rec is None:
                    msg.set(_("No markers in view - the empty-bed photo was not saved."))
                    return
                save_background(rec)
                bg_state.set(_("Empty-bed photo: saved"))
                msg.set(_("Saved. The lid is now found as whatever differs from it."))

            self._start(work, done, lambda e: msg.set(_("Camera: %s") % e))

        def drop_background():
            clear_background()
            bg_state.set(_("Empty-bed photo: none"))
            msg.set(_("Removed. The lid is found by its edges."))

        def calibrate_size():
            if busy_msg():
                return
            i, row = f._selected()
            lid = f._lid_for(i) if row is not None else None
            gen = f._gen()
            if not hasattr(gen, "_lid_defaults"):
                gen._lid_defaults = gen._load_lid_defaults()
            want = PillcaseOrder.lid_dimensions(gen._lid_defaults.get(lid)) if lid else None
            if not want:
                msg.set(_("Select a lid in the tote list, with its size configured, and "
                          "place that lid in the fixture first."))
                return
            msg.set(_("Measuring %s...") % lid)
            mm_v = marker_mm()
            bg = load_background()

            def work():
                shot = self._photo()
                return LidVision.identify(shot.jpeg, [], lambda k: None, mm_v, 1.0, bg)

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

        def forget_colours():
            if messagebox.askyesno(_("Forget Colours"),
                                   _("Forget every colour learned from confirmed lids? Matching "
                                     "goes back to the thumbnails."), parent=d):
                self.colours.forget()
                msg.set(_("Learned colours cleared."))

        row = Frame(tools)
        row.pack(side=TOP, fill=X, pady=(4, 0))
        Button(row, text=_("Save Empty-Bed Photo"), command=take_background).pack(side=LEFT, padx=2)
        Button(row, text=_("Remove"), command=drop_background).pack(side=LEFT, padx=2)
        row = Frame(tools)
        row.pack(side=TOP, fill=X, pady=(4, 0))
        Button(row, text=_("Calibrate Size with Selected Lid"), command=calibrate_size).pack(
            side=LEFT, padx=2)
        Button(row, text=_("Forget Learned Colours (%d)") % len(self.colours.colours),
               command=forget_colours).pack(side=LEFT, padx=2)

        Label(body, textvariable=msg, fg="DarkBlue", wraplength=420, justify=LEFT).grid(
            row=7, column=0, columnspan=2, sticky=W, pady=(6, 0))

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
            Utils.setStr(_SECTION, "markerMM", "%g" % mm_v)
            Utils.setStr(_SECTION, "sizeScale", "%.4f" % scale_v)
            d.destroy()
            f.update_camera_button()

        bf = Frame(body)
        bf.grid(row=8, column=0, columnspan=2, sticky="e", pady=(10, 0))
        Button(bf, text=_("Save"), width=12, command=save).pack(side=LEFT, padx=5)
        Button(bf, text=_("Cancel"), width=12, command=d.destroy).pack(side=LEFT)

        f._gen()._fit_and_center(d)
        d.deiconify()
        d.grab_set()
        f.wait_window(d)
