"""Tote Scanning: engraver login and the open totes' lists of lids.

This frame is the Tote Scanning section of the Lid Engravings tab (SurfAlignPage).
The loop at the machine:
  1. The engraver logs in here - tap a name, or scan / type it - as at the logger's
     station tablet. A scan with nobody logged in lands here first.
  2. A tote/order scan is looked up in ShipHero and its lids are listed here,
     under the tote, alongside any other totes still open.
  3. The G-code view shows the lid camera's view of the bed (LidCamera). A lid
     placed in the fixture is identified and selected here, and its preview
     made at once: the engraving on a photo of that lid, beside a photo of the
     same lid from the camera's library. Confirm & Engrave runs Align & Run.
     Picking another lid here makes its preview instead, with the camera's
     Retake / Save to Library beside it.
  4. Without the camera, Confirm Lid fills the G-code fields and generates the
     preview; the same button, now Confirm & Engrave, runs Align & Run.
  5. When that program finishes without error the lid is crossed off, a completion
     record is handed to EngravingLog, and the next lid is selected. A tote whose
     lids are all done closes by itself a moment later; Close Tote closes one
     sooner. Lids done on another station cross off here too (LanSync).
"""

import socket
import time
from tkinter import (
    BOTH, END, LEFT, RIGHT, TOP, NW, NE, E, W, X, Y, YES, VERTICAL,
    BooleanVar, Button, Checkbutton, Entry, Frame, Label, LabelFrame, Scrollbar,
    StringVar, Toplevel, messagebox,
)
from tkinter import font as tkFont
from tkinter import ttk
import tkinter as tk

import CNCRibbon
import EngravingLog
import EngravingSession as ES
import LanSync
import LidCamera
import PillcaseOrder
import Utils
from Utils import _

_SECTION = "Engraving"

# A tote whose lids are all done closes this long after, so the last lid's
# cross-off is seen first.
TOTE_CLOSE_MS = 5000
# Keys typed into the lid list within this long before Enter are a barcode
# scan, not a press of the engrave button.
SCAN_KEYS_S = 1.0
# The lid camera makes the selected lid's preview this long after the
# selection settles.
PREPARE_DELAY_MS = 300


class OpenTote:
    """A scanned tote or order, listed until it is done or closed."""

    _ids = 0

    def __init__(self, scan, mode, rows):
        OpenTote._ids += 1
        self.id = OpenTote._ids             # names its rows in the list
        self.scan = scan
        self.mode = mode
        self.key = ES.scan_key(mode, scan)
        self.rows = list(rows)
        self.row_keys = ES.row_keys(self.rows)
        self.lid_override = {}              # row index -> lid picked by hand
        self.closing = None                 # after() id of its pending close

    @property
    def noun(self):
        return _("Tote") if self.mode == "Tote" else _("Order")

    def engravings(self):
        return [i for i, r in enumerate(self.rows) if ES.has_engraving(r)]


class EngravingFrame(CNCRibbon.PageFrame):

    def __init__(self, master, app):
        CNCRibbon.PageFrame.__init__(self, master, "Engraving", app)
        self.app.engraving = self

        self.session = ES.Session()
        self.progress = ES.ProgressStore(Utils.iniUser + "-engraving-progress.json")
        LanSync.start(self.progress, self.machine_name())

        self.totes = []             # OpenTote, newest first
        self.odds = {}              # (tote key, row key) -> the camera's odds
        self._armed = None          # (tote id, row): previewed, the button now starts it
        self._camera_prompt = None  # (text, colour) from the lid camera
        self._prepare_job = None    # a preview to make for the lid on the bed (camera_ready)
        self._typed = ("", 0.0)     # keys typed into the list: a scan in progress

        self.active_job = None      # the lid last sent to SurfAlign from this list
        self._generated = None      # what the loaded G-code was generated from
        self._run = None            # snapshot of the program run in progress
        self._pending_scan = None   # scanned before anyone logged in
        self._icon_cache = {}
        self.lidcam = LidCamera.Controller(self)

        self._build()
        self._show_session()
        self.after(1000, self._tick)

    # ================================================================ config
    @staticmethod
    def machine_name():
        return Utils.getStr(_SECTION, "machineName", "").strip() or socket.gethostname()

    @staticmethod
    def roster():
        return ES.parse_roster(Utils.getStr(_SECTION, "engravers", ES.DEFAULT_ENGRAVERS))

    @staticmethod
    def idle_seconds():
        minutes = Utils.getFloat(_SECTION, "idleMinutes", ES.DEFAULT_IDLE_MINUTES)
        return (minutes if minutes > 0 else ES.DEFAULT_IDLE_MINUTES) * 60

    def _gen(self):
        return self.app.surfalign_gen_gcode_frame

    def _status(self, msg, error=False):
        self._gen()._scan_status(msg, error)

    def _page(self):
        return getattr(self.app, "lid_engravings", None)

    def _show(self):
        """Bring Tote Scanning forward, open and scrolled into view."""
        page = self._page()
        if page is not None:
            page.show_scanning()

    # ================================================================== UI
    def _build(self):
        # Engraver name, shift stats and Log out: shown only while logged in.
        # Station settings are under Advanced Settings (LidEngravingsFrame).
        self.session_box = Frame(self)
        bar = Frame(self.session_box)
        bar.pack(side=TOP, fill=X, pady=(4, 0))
        self.who_var = StringVar()
        Label(bar, textvariable=self.who_var, font=("", 12, "bold"), anchor=W).pack(side=LEFT)
        Button(bar, text=_("Log out"), command=self.logout).pack(side=RIGHT, padx=2)
        # Other stations on the network, seen via LanSync - just a dot and a
        # count, packed right after (so immediately left of) Log out.
        self.peers_var = StringVar()
        self.peers_lbl = Label(bar, textvariable=self.peers_var, font=("", 9))
        self.peers_lbl.pack(side=RIGHT, padx=(0, 6))
        self.stats_var = StringVar()
        Label(self.session_box, textvariable=self.stats_var, anchor=W, fg="gray").pack(side=TOP, fill=X)

        self._build_login()
        self._build_work()

    def _build_login(self):
        # Name buttons, plus an entry for a badge scan or a typed name
        box = self.login_box = Frame(self)
        self.names_frame = Frame(box)
        self.names_frame.pack(fill=X, pady=(6, 4))
        f = Frame(box)
        f.pack(fill=X, pady=(0, 4))
        self.badge = Entry(f, font=("", 13))
        self.badge.pack(side=LEFT, fill=X, expand=YES)
        self.badge.bind("<Return>", self._login_from_entry)
        Button(f, text=_("Start"), command=self._login_from_entry).pack(side=LEFT, padx=(4, 0))

    def _build_work(self):
        box = self.work_box = Frame(self)

        sf = Frame(box)
        sf.pack(side=TOP, fill=X, pady=(4, 2))
        self.scan_label = Label(sf, text=self._scan_label_text())
        self.scan_label.pack(side=LEFT)
        self.scan_entry = Entry(sf, font=("", 12))
        self.scan_entry.pack(side=LEFT, fill=X, expand=YES, padx=4)
        self.scan_entry.bind("<Return>", self.scan)
        self.addWidget(self.scan_entry)
        b = Button(sf, text="🔍", width=2, command=self.scan)
        b.pack(side=LEFT)
        self.addWidget(b)

        # What to do next: scan, place a lid, confirm, check the preview...
        self.prompt_var = StringVar()
        self.prompt_lbl = Label(box, textvariable=self.prompt_var, font=("", 11, "bold"),
                                anchor=W, justify=LEFT, wraplength=300)
        self.prompt_lbl.pack(side=TOP, fill=X)
        self.prompt_lbl.bind("<Configure>", lambda e: self.prompt_lbl.config(
            wraplength=max(200, e.width - 8)))

        # Above the list, so it stays on screen however short the window is: Tk
        # squeezes the last-packed widgets (the details) first. Pressed once it
        # shows the preview; pressed again it starts the engraving.
        self.engrave_btn = Button(box, command=self.engrave_selected, fg="white",
                                  activeforeground="white", font=("", 12, "bold"), pady=6)
        self.engrave_btn.pack(side=TOP, fill=X, pady=(4, 0))
        self.engrave_btn.bind("<Return>", self.engrave_selected)
        self.addWidget(self.engrave_btn)
        self._update_engrave_button()
        bar = self.tools_bar = Frame(box)
        bar.pack(side=TOP, fill=X, pady=(2, 0))
        # Takes a photo now, whatever the camera was waiting for (LidCamera).
        self.camera_btn = Button(bar, text=_("📷  Look Again"), command=lambda: self.lidcam.look(force=True))
        self.addWidget(self.camera_btn)
        self.close_btn = Button(bar, text=_("Close Tote"), command=self.close_selected_tote)
        self.close_btn.pack(side=RIGHT)
        self.warn_var = StringVar()
        warn_lbl = Label(box, textvariable=self.warn_var, fg="red", anchor=W, justify=LEFT,
                         wraplength=300)
        # Takes a line only while there is a warning to show
        self.warn_var.trace_add("write", lambda *a: warn_lbl.pack(
            side=TOP, fill=X, after=self.tools_bar) if self.warn_var.get() else warn_lbl.pack_forget())

        style = ttk.Style()
        # Rows as tall as the text (and its icon): a fixed height clips larger fonts.
        line = tkFont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("EngravingItems.Treeview", rowheight=max(24, line + 8))
        style.layout("EngravingItems.Treeview.Item", [
            ("Treeitem.padding", {"sticky": "nswe", "children": [
                ("Treeitem.indicator", {"side": "left", "sticky": ""}),
                ("Treeitem.image", {"side": "left", "sticky": ""}),
                ("Treeitem.text", {"sticky": "nswe"}),
            ]})
        ])
        tc = Frame(box)
        # Fixed and short, so the preview controls and Align & Run below Tote
        # Scanning stay on screen; more lids scroll the list.
        tc.pack(side=TOP, fill=X, pady=2)
        tree = self.tree = ttk.Treeview(tc, style="EngravingItems.Treeview",
                                        columns=("Engraving", "Camera", "Order"),
                                        show="tree headings", height=8)
        tree.heading("#0", text=_("Case Type / Colour"))
        tree.column("#0", width=170, minwidth=120)
        tree.heading("Engraving", text=_("Engraving"))
        tree.column("Engraving", width=80, minwidth=50)
        tree.heading("Camera", text="📷")
        odds_w = tkFont.nametofont("TkDefaultFont").measure("100%") + 12
        tree.column("Camera", width=odds_w, minwidth=odds_w, anchor=E, stretch=False)
        tree.heading("Order", text=_("Order"))
        tree.column("Order", width=60, minwidth=45)
        # Scrollbar packed first: packed after, a wide list squeezes it out.
        sb = Scrollbar(tc, orient=VERTICAL, command=tree.yview)
        sb.pack(side=RIGHT, fill=Y)
        tree.pack(side=LEFT, fill=BOTH, expand=YES)
        tree.configure(yscrollcommand=sb.set)

        done_font = tkFont.nametofont("TkDefaultFont").copy()
        done_font.configure(overstrike=1)
        self._done_font = done_font     # Tk drops a font nobody holds
        tote_font = tkFont.nametofont("TkDefaultFont").copy()
        tote_font.configure(weight="bold")
        self._tote_font = tote_font
        tree.tag_configure("tote", font=tote_font, background="#dde6ee")
        tree.tag_configure("tote_done", foreground="#1a7f37")
        tree.tag_configure("done", foreground="gray", font=done_font)
        tree.tag_configure("unmapped", background="#ffcccc")
        tree.tag_configure("warned", background="#fff0cc")
        tree.tag_configure("likely", background="#d4f0d4")

        tree.bind("<<TreeviewSelect>>", self._on_select)
        tree.bind("<Double-1>", self.engrave_selected)
        tree.bind("<Return>", self._on_list_return)
        tree.bind("<KP_Enter>", self._on_list_return)
        # A barcode scanner types into whatever has focus: typed here, it is
        # collected and handed to the scan field on Enter, never reaching
        # bCNC's own key bindings (1-3 set the jog step, ! holds the feed).
        tree.bind("<Key>", self._on_list_key)
        # ttk.Treeview's own Up/Down bindings don't stop the key from also
        # reaching bCNC's toplevel jog bindings (acceptKey() doesn't know about
        # Treeview), so without an explicit "break" here, arrow-keying through
        # this list jogs the mill. Left/Right are just swallowed.
        tree.bind("<Up>", lambda e: self._move_selection(-1))
        tree.bind("<Down>", lambda e: self._move_selection(1))
        tree.bind("<Left>", lambda e: "break")
        tree.bind("<Right>", lambda e: "break")
        self.update_camera_button()
        self._update_prompt()

        self.hide_non_engraving = BooleanVar(value=True)
        Checkbutton(box, text=_("Hide non-engraving items"), variable=self.hide_non_engraving,
                    command=self._refresh_tree).pack(side=TOP, anchor=W)

        self._build_details(box)

    def _build_details(self, parent):
        d = LabelFrame(parent, text=_("Item Details"), padx=8, pady=4)
        d.pack(side=TOP, fill=X, pady=4)
        d.columnconfigure(1, weight=1)
        self.d_case = StringVar()
        self.d_colour = StringVar()
        self.d_engraving = StringVar()
        self.d_lid = StringVar()
        self.d_source = StringVar()
        self.d_warnings = StringVar()

        def row(label, var, r):
            Label(d, text=label, font=("", 9, "bold")).grid(row=r, column=0, sticky=NE, padx=(0, 5), pady=2)
            lbl = Label(d, textvariable=var, justify=LEFT, anchor=NW, wraplength=260)
            lbl.grid(row=r, column=1, sticky=NW, pady=2)
            return lbl

        self.case_lbl = row(_("Case Type:"), self.d_case, 0)
        self.case_lbl.config(font=("", 10, "bold"))

        Label(d, text=_("Colour:"), font=("", 9, "bold")).grid(row=1, column=0, sticky=NE, padx=(0, 5), pady=2)
        cf = Frame(d)
        cf.grid(row=1, column=1, sticky=NW, pady=2)
        self.colour_lbl = Label(cf, textvariable=self.d_colour, justify=LEFT, anchor=NW)
        self.colour_lbl.pack(side=LEFT)
        self.swatch = tk.Canvas(cf, width=36, height=36, highlightthickness=1, highlightbackground="gray")

        row(_("Engraving:"), self.d_engraving, 2)

        Label(d, text=_("Lid:"), font=("", 9, "bold")).grid(row=3, column=0, sticky=NE, padx=(0, 5), pady=2)
        self.lid_combo = ttk.Combobox(d, state="readonly", textvariable=self.d_lid, width=25)
        self.lid_combo.grid(row=3, column=1, sticky=NW, pady=2)
        self.lid_combo.bind("<<ComboboxSelected>>", self._on_lid_override)

        src = row(_("From:"), self.d_source, 4)
        src.config(fg="gray", font=("", 8))
        Label(d, textvariable=self.d_warnings, justify=LEFT, anchor=NW, fg="red",
              font=("", 8), wraplength=320).grid(row=5, column=0, columnspan=2, sticky=NW)

    def update_camera_button(self):
        """Show Look Again and the odds only while the lid camera is switched on."""
        if LidCamera.enabled():
            self.camera_btn.pack(side=LEFT)
            self.tree.configure(displaycolumns=("Engraving", "Camera", "Order"))
        else:
            self.camera_btn.pack_forget()
            self.tree.configure(displaycolumns=("Engraving", "Order"))
            self.show_odds({}, None)
            self.set_camera_prompt(None)

    def _scan_label_text(self):
        mode = Utils.getStr("SurfAlign", "shipheroSearchMode", "Order")
        return _("Order Number:") if mode == "Order" else _("Tote ID:")

    # ============================================================== session
    def _render_names(self):
        for w in self.names_frame.winfo_children():
            w.destroy()
        for i, name in enumerate(self.roster()):
            Button(self.names_frame, text=name, font=("", 12, "bold"), pady=12,
                   bg="#24506f", fg="white", activebackground="#2E75B6", activeforeground="white",
                   command=lambda n=name: self.login(n)).grid(
                row=i // 2, column=i % 2, sticky="nsew", padx=3, pady=3)
        self.names_frame.columnconfigure(0, weight=1)
        self.names_frame.columnconfigure(1, weight=1)

    def _show_session(self):
        if self.session.logged_in:
            self.who_var.set("▣ " + self.session.engraver)
            self.login_box.pack_forget()
            self.session_box.pack(side=TOP, fill=X)
            self.scan_label.config(text=self._scan_label_text())
            self.work_box.pack(side=TOP, fill=BOTH, expand=YES)
        else:
            self.session_box.pack_forget()
            self.work_box.pack_forget()
            self._render_names()
            self.login_box.pack(side=TOP, fill=BOTH, expand=YES)
        self._update_stats()

    def _login_from_entry(self, event=None):
        self.login(self.badge.get())
        return "break"

    def login(self, name):
        name = (name or "").strip()
        if not name:
            return
        if not ES.valid_engraver_name(name):
            # Leave the bad value selected, so the next scan or typing replaces it
            self.bell()
            self.badge.focus_set()
            self.badge.select_range(0, END)
            return
        self.badge.delete(0, END)
        self.session.login(name)
        self._show_session()
        self._status(_("✓ %s logged in") % name)

        pending, self._pending_scan = self._pending_scan, None
        if pending:
            self._gen().fetchShipHeroOrder(pending)
        else:
            self.focus_scan()

    def logout(self, auto=False):
        who = self.session.engraver
        self.session.logout()
        self._show_session()
        if who:
            self._status(_("%s logged out after inactivity") % who if auto
                         else _("%s logged out - nice work") % who)
        self.badge.focus_set()

    def require_login(self, pending_scan=None):
        """A scan arrived with nobody logged in: ask for a name, keep the scan."""
        self._pending_scan = pending_scan or None
        self._show()
        self._show_session()
        self.bell()
        self.badge.focus_set()

    def _tick(self):
        try:
            if self.app.running:
                # A long cut is the engraver working, not an idle station.
                self.session.touch()
            elif self.session.logged_in and self.session.is_idle(self.idle_seconds()):
                self.logout(auto=True)
            self._update_stats()
            if LanSync.drain(self.progress):
                # Lids done on a peer cross off here, and a tote it finished
                # closes here too - without yanking focus away from whatever
                # this station's operator is doing right now.
                self._refresh_tree()
            self._update_peer_label()
        except Exception as e:
            print("[engraving] tick error:", repr(e))
        self.after(1000, self._tick)

    def _update_peer_label(self):
        n = LanSync.peer_count()
        self.peers_var.set("● %d" % n)
        self.peers_lbl.config(fg="#1a7f37" if n else "gray")

    def _update_stats(self):
        s = self.session
        if not s.logged_in:
            return
        secs = int((ES.now_utc() - s.since).total_seconds())
        h, m, sec = secs // 3600, secs % 3600 // 60, secs % 60
        elapsed = "%d:%02d:%02d" % (h, m, sec) if h else "%d:%02d" % (m, sec)
        rate = s.rate_per_hour()
        self.stats_var.set(_("%d engraved this shift  ·  %s on shift  ·  %s / hour")
                           % (s.count, elapsed, "%.1f" % rate if rate else "—"))

    # ================================================================ scan
    def focus_scan(self):
        """Put the operator on the scan field, old value selected so a scan replaces it."""
        self._show()
        if not self.session.logged_in:
            self.badge.focus_set()
            return
        self.scan_entry.focus_set()
        self.scan_entry.select_range(0, END)
        self.scan_entry.icursor(END)

    def scan(self, event=None):
        self._gen().fetchShipHeroOrder(self.scan_entry.get())
        return "break"

    def load_order(self, scan_value, search_mode, rows):
        """List a looked-up tote/order at the top, ready to pick a lid. A tote
        already open is refreshed in place; one with nothing to engrave leaves
        when the next is scanned."""
        scan_value = str(scan_value or "").strip()
        tote = OpenTote(scan_value, search_mode, rows)
        old = self.tote(tote.key)
        if old is not None:
            # Rescanned: the same lids, its hand-picked lids kept
            tote.id, tote.lid_override = old.id, old.lid_override
            self._cancel_close(old)
        self.totes = [tote] + [t for t in self.totes if t.key != tote.key and t.engravings()]
        self.session.touch()

        self.scan_entry.delete(0, END)
        self.scan_entry.insert(0, scan_value)
        self.warn_var.set("")
        self._refresh_tree()
        self._show()
        self._select_next(tote)
        self.lidcam.totes_changed()

    # ================================================================ totes
    def tote(self, key):
        return next((t for t in self.totes if t.key == key), None)

    def _tote_by_id(self, tote_id):
        return next((t for t in self.totes if t.id == tote_id), None)

    def _completed(self, tote):
        return self.progress.completed(tote.key)

    def _remaining(self, tote):
        done = self._completed(tote)
        return [i for i in tote.engravings() if tote.row_keys[i] not in done]

    def waiting(self):
        """Every lid still to engrave: [(tote, row index)], newest tote first."""
        return [(t, i) for t in self.totes for i in self._remaining(t)]

    def focus_tote(self):
        """The tote being worked on: the selected lid's, else the newest."""
        tote, _i, _row = self._selected()
        if tote is None:
            sel = self.tree.selection()
            tote = self._tote_by_id(int(sel[0][1:])) if sel and sel[0].startswith("t") else None
        return tote or (self.totes[0] if self.totes else None)

    def close_tote(self, tote):
        self._cancel_close(tote)
        if tote not in self.totes:
            return
        was_selected = self._selected()[0] is tote
        self.totes.remove(tote)
        if self._armed and self._armed[0] == tote.id:
            self._disarm()
        self._refresh_tree()
        if was_selected or not self.tree.selection():
            self._select_next()
        self.lidcam.totes_changed()

    def close_selected_tote(self):
        tote = self.focus_tote()
        if tote is None:
            return
        left = len(self._remaining(tote))
        if left and not messagebox.askyesno(
                _("Close Tote"),
                _("%s %s still has %d lid(s) to engrave.\n\nClose it anyway?")
                % (tote.noun, tote.scan, left), parent=self):
            return
        self.close_tote(tote)
        self._status(_("%s %s closed.") % (tote.noun, tote.scan))

    def _cancel_close(self, tote):
        if tote.closing is not None:
            self.after_cancel(tote.closing)
            tote.closing = None

    def _schedule_closes(self):
        """Close each finished tote shortly: done here, or on another station."""
        for t in self.totes:
            if t.closing is None and t.engravings() and not self._remaining(t):
                t.closing = self.after(TOTE_CLOSE_MS, lambda t=t: self._close_if_done(t))

    def _close_if_done(self, tote):
        tote.closing = None
        if tote in self.totes and not self._remaining(tote):
            self.close_tote(tote)

    # ================================================================ list
    def _lid_for(self, tote, i):
        if i in tote.lid_override:
            return tote.lid_override[i]
        gen = self._gen()
        if not hasattr(gen, "_lid_defaults"):
            gen._lid_defaults = gen._load_lid_defaults()
        hits = PillcaseOrder.lids_for_product(gen._lid_defaults, tote.rows[i].get("product_code"))
        return hits[0] if hits else None

    def _colour_icon(self, row, size=18):
        code = row.get("colour_code") or ""
        name = row.get("colour_name") or ""
        if not code and not name:
            return None
        key = "%s|%s|%s" % (code.lower(), name.lower(), size)
        if key in self._icon_cache:
            return self._icon_cache[key]
        path = PillcaseOrder.find_thumbnail(
            PillcaseOrder.thumbnail_dirs(Utils.getStr("SurfAlign", "thumbnailsDir", "")), code, name)
        if not path:
            self._icon_cache[key] = None
            return None
        try:
            from PIL import Image, ImageTk
        except Exception:
            return None
        try:
            icon = ImageTk.PhotoImage(Image.open(path).resize((size, size), Image.LANCZOS))
        except Exception:
            icon = None
        self._icon_cache[key] = icon
        return icon

    def _header_text(self, tote):
        total = len(tote.engravings())
        left = len(self._remaining(tote))
        if not total:
            return _("%s %s - nothing to engrave") % (tote.noun, tote.scan)
        if not left:
            return _("✓ %s %s - all %d done") % (tote.noun, tote.scan, total)
        return _("%s %s - %d/%d done") % (tote.noun, tote.scan, total - left, total)

    @staticmethod
    def _iid(tote, i):
        return "%d:%d" % (tote.id, i)

    def _refresh_tree(self):
        tree = self.tree
        selected = tree.selection()
        tree.delete(*tree.get_children())
        for tote in self.totes:
            done = self._completed(tote)
            head = "t%d" % tote.id
            # The tote's name where the lids' names go; its progress beside it
            total, left = len(tote.engravings()), len(self._remaining(tote))
            tree.insert("", END, iid=head, open=True,
                        text=("✓ " if total and not left else "") + "%s %s" % (tote.noun, tote.scan),
                        values=(_("%d/%d done") % (total - left, total) if total
                                else _("nothing to engrave"), "", ""),
                        tags=("tote",) if self._remaining(tote) or not tote.engravings()
                        else ("tote", "tote_done"))
            for i, row in enumerate(tote.rows):
                engraving = ES.has_engraving(row)
                if self.hide_non_engraving.get() and not engraving:
                    continue
                finished = engraving and tote.row_keys[i] in done
                odds = None if finished else self.odds.get((tote.key, tote.row_keys[i]))
                if finished:
                    tags = ("done",)
                elif not self._lid_for(tote, i):
                    tags = ("unmapped",)
                elif row.get("warnings"):
                    tags = ("warned",)
                elif odds is not None and odds == max(self.odds.values()):
                    tags = ("likely",)
                else:
                    tags = ()
                case = row.get("case_type") or row.get("product_code") or _("Unknown case")
                text = "%s  –  %s" % (case, row.get("colour_name") or _("colour?"))
                if finished:
                    text = "✓ " + text
                icon = self._colour_icon(row)
                kw = {"image": icon, "text": "  " + text} if icon else {"text": text}
                tree.insert(head, END, iid=self._iid(tote, i),
                            values=(row.get("engraving") or "",
                                    "" if odds is None else "%d%%" % round(100 * odds),
                                    row.get("order_number") or ""),
                            tags=tags, **kw)
        for iid in selected:
            if tree.exists(iid):
                tree.selection_set(iid)
        self._schedule_closes()
        self._update_prompt()

    def _row_iids(self):
        """The lids' rows in the list, in order, without the tote headings."""
        return [iid for head in self.tree.get_children() for iid in self.tree.get_children(head)]

    def _move_selection(self, delta):
        """Up/Down in the list, in place of the mill-jog binding they'd otherwise hit."""
        rows = self._row_iids()
        if not rows:
            return "break"
        current = self.tree.focus() or (self.tree.selection() or (None,))[0]
        idx = rows.index(current) if current in rows else -1
        idx = max(0, min(len(rows) - 1, idx + delta))
        self.select_row(rows[idx])
        return "break"

    def select_row(self, iid, focus=False):
        tree = self.tree
        if not tree.exists(iid):
            return
        tree.selection_set(iid)
        tree.focus(iid)
        tree.see(iid)
        if focus:
            tree.focus_set()

    def _select_next(self, tote=None):
        """The first lid still to engrave: in `tote` if it has one, else in any."""
        waiting = [(t, i) for t, i in self.waiting() if tote is None or t is tote] or self.waiting()
        rows = self._row_iids()
        target = self._iid(*waiting[0]) if waiting else (rows[0] if rows else None)
        if target is None:
            self.tree.selection_set(())
            self._on_select()
            return
        self.select_row(target, focus=True)

    def _selected(self):
        """(tote, row index, row) of the selected lid, or Nones."""
        sel = self.tree.selection()
        if not sel or sel[0].startswith("t"):
            return None, None, None
        tote_id, i = (int(v) for v in sel[0].split(":"))
        tote = self._tote_by_id(tote_id)
        if tote is None:
            return None, None, None
        return tote, i, tote.rows[i]

    def _on_select(self, event=None):
        self.warn_var.set("")
        tote, i, row = self._selected()
        if self._armed and (tote is None or self._armed != (tote.id, i)):
            self._disarm()
        self.swatch.delete("all")
        if row is None:
            for var in (self.d_case, self.d_colour, self.d_engraving, self.d_lid,
                        self.d_source, self.d_warnings):
                var.set("")
            self.swatch.pack_forget()
            return
        self.d_case.set(row.get("case_type") or row.get("product_code") or _("Unknown case"))
        self.case_lbl.config(fg="black" if row.get("case_type") else "red")
        self.d_colour.set(row.get("colour_name") or _("Not resolved"))
        self.colour_lbl.config(fg="black" if row.get("colour_name") else "red")
        icon = self._colour_icon(row, size=36)
        if icon:
            self.swatch.pack(side=LEFT, padx=(10, 0))
            self.swatch.image = icon
            self.swatch.create_image(0, 0, anchor=NW, image=icon)
        else:
            self.swatch.pack_forget()
        self.d_engraving.set(row.get("engraving") or "")
        self.lid_combo.config(values=list(getattr(self._gen(), "lid_list", [])))
        self.d_lid.set(self._lid_for(tote, i) or "")
        self.d_source.set("%s %s  |  %s  |  %s  |  %s" % (
            tote.noun, tote.scan, row.get("product_name") or "",
            row.get("sku") or _("no SKU"), row.get("rule") or ""))
        self.d_warnings.set("\n".join(row.get("warnings") or []))
        if self.lidcam.lid_on_bed():
            self.camera_ready()

    def _on_lid_override(self, event=None):
        tote, i, row = self._selected()
        if row is None:
            return
        lid = self.d_lid.get()
        if lid:
            tote.lid_override[i] = lid
            self._disarm()
            self._refresh_tree()
            if self.lidcam.lid_on_bed():
                self.camera_ready()

    def _on_list_key(self, event):
        if event.char and event.char.isprintable() and not event.state & 0x4:   # not Ctrl
            text, at = self._typed
            now = time.monotonic()
            self._typed = ((text if now - at < SCAN_KEYS_S else "") + event.char, now)
            return "break"

    def _on_list_return(self, event=None):
        text, at = self._typed
        self._typed = ("", 0.0)
        if text and time.monotonic() - at < SCAN_KEYS_S:
            self.scan_entry.delete(0, END)
            self.scan_entry.insert(0, text)
            return self.scan()
        return self.engrave_selected()

    # ======================================================= camera, prompt
    def show_odds(self, odds, best):
        """The lid camera's odds of each waiting lid ({(tote key, row key): p}),
        and its best guess, (tote key, row key), selected."""
        self.odds = dict(odds)
        self._refresh_tree()
        tote = self.tote(best[0]) if best else None
        if tote is not None and best[1] in tote.row_keys:
            # Into the list only from the scan field or the list itself: never
            # away from a preview field being typed in.
            focus = self.focus_get()
            self.select_row(self._iid(tote, tote.row_keys.index(best[1])),
                            focus=focus in (None, self.tree, self.scan_entry))

    def camera_ready(self):
        """A lid sits on the bed and a lid is selected: make its preview, once
        the selection settles (arrowing through the list makes only the last)."""
        if self._prepare_job is not None:
            self.after_cancel(self._prepare_job)
        self._prepare_job = self.after(PREPARE_DELAY_MS, self._prepare_for_camera)

    def _prepare_for_camera(self):
        self._prepare_job = None
        tote, i, row = self._selected()
        if row is None or not self.lidcam.lid_on_bed():
            return
        if self._armed == (tote.id, i):
            self.lidcam.show_panel(tote, i)
            return
        if self.prepare_selected(quiet=True):
            self._armed = (tote.id, i)
            self._update_engrave_button()
            self._update_prompt()

    def set_camera_prompt(self, text, colour="black"):
        self._camera_prompt = (text, colour) if text else None
        self._update_prompt()

    def _update_prompt(self):
        if self._armed and self._camera_prompt and self.lidcam.lid_on_bed():
            text, colour = self._camera_prompt
        elif self._armed:
            text, colour = _("Check the preview, then press Confirm & Engrave."), "#1F3B57"
        elif not self.totes:
            text, colour = _("Scan a tote to list its engravings."), "black"
        elif self._camera_prompt:
            text, colour = self._camera_prompt
        elif not self.waiting():
            text, colour = _("All open totes are done - scan the next one."), "#1a7f37"
        else:
            text, colour = _("Pick the lid in the fixture, then Confirm Lid."), "black"
        self.prompt_var.set(text)
        self.prompt_lbl.config(fg=colour)

    # ============================================================= engrave
    def armed(self):
        return self._armed is not None

    def _disarm(self):
        if self._armed is not None:
            self._armed = None
            self._update_engrave_button()
            self._update_prompt()

    def _update_engrave_button(self):
        if self._armed:
            self.engrave_btn.config(text=_("✓  Confirm & Engrave  ▶"), bg="#4CAF50",
                                    activebackground="#45a049")
        else:
            self.engrave_btn.config(text=_("✓  Confirm Lid  ▶"), bg="#2E75B6",
                                    activebackground="#1F3B57")

    def engrave_selected(self, event=None):
        """The engrave button: the first press shows the selected lid's
        preview, the second (with the same lid selected) starts it."""
        tote, i, row = self._selected()
        if self._armed and tote is not None and self._armed == (tote.id, i):
            self.start_engraving()
        elif self.prepare_selected():
            self._armed = (tote.id, i)
            self._update_engrave_button()
            self._update_prompt()
            self.tree.focus_set()
        return "break"

    def start_engraving(self):
        page = self._page()
        multipoint = getattr(page, "multipoint", None) if page is not None else None
        if self.app.running or (page is not None and page.busy()):
            self._warn(_("The machine is running - wait for it to finish."))
            return
        tote, i, row = self._selected()
        if row is not None:
            # A lid the camera picked by itself teaches it, now it is confirmed
            self.lidcam.note_engraving(tote, i)
        self.lidcam.view.hide_all()
        self._disarm()
        if multipoint is None:
            self._status(_("Ready - press Align & Run."))
            return
        # A look at the bed still in flight finishes before probing starts.
        self.lidcam.when_idle(multipoint.quick_align_run)

    def prepare_selected(self, quiet=False):
        """Fill the G-code fields from the selected lid and generate its
        preview, saying whether the G-code is ready to run. `quiet`: made for
        the lid camera, unasked - a lid already engraved is not offered again."""
        tote, i, row = self._selected()
        if row is None:
            self._warn(_("Select a case first."))
            return False
        page = self._page()
        if self.app.running or (page is not None and page.busy()):
            self._warn(_("The machine is running - wait for it to finish."))
            return False
        if not ES.has_engraving(row):
            self._warn(_("This item has no Lid Engraving text."))
            return False
        lid = self._lid_for(tote, i)
        if not lid:
            self._warn(_("This case type is not mapped to any Lid. Pick one from the Lid dropdown."))
            return False
        key = tote.row_keys[i]
        if key in self._completed(tote) and (quiet or not messagebox.askyesno(
                _("Already Engraved"),
                _("This lid is already marked as engraved.\n\nEngrave it again?"), parent=self)):
            return False

        text = str(row.get("engraving"))
        gen = self._gen()
        gen.engraveText.set(text)
        gen.lidName.set(lid)
        gen._apply_defaults_to_main_fields_if_available(lid)
        # The lid's own photo, faintly under the engraving (none without one)
        self.lidcam.show_backdrop(lid, gen.get_lid_dimensions())

        self.active_job = {
            "tote_key": tote.key, "row_key": key, "row": dict(row), "lid": lid,
            "scan": tote.scan, "search_mode": tote.mode,
        }
        self.session.touch()
        self.warn_var.set("")

        # Loading G-code only skips the probe-data prompt with this page active.
        self.app.ribbon.changePage("SurfAlign")
        self._status(_("Imported '%s' on %s - generating GCode...") % (text, lid))
        try:
            ok = gen.generateGcode()
        except Exception as e:
            self._status(_("GCode generation failed: ") + str(e), error=True)
            return False
        if ok:
            self._status(_("Ready - check the preview for '%s' on %s, then press Confirm & Engrave.")
                         % (text, lid))
            if self.lidcam.lid_on_bed():
                self.lidcam.show_panel(tote, i)
            else:
                self.lidcam.view.hide_panel()
            if page is not None:
                page.see(self.engrave_btn)
        else:
            self._status(_("GCode generation failed - fields imported, not generated."), error=True)
        return bool(ok)

    def _warn(self, msg):
        self.warn_var.set(msg)
        self.bell()

    # ============================================= hooks from bCNC / SurfAlign
    def on_gcode_generated(self, text, lid):
        """SurfAlign generated and loaded G-code from these fields."""
        self._generated = {"text": text, "lid": lid, "job": self.active_job}

    def on_gcode_loaded(self):
        """Any G-code file was loaded; it is generated only if SurfAlign says so next."""
        self._generated = None

    def on_run_started(self, program):
        """A run is starting. Only a program run of generated G-code can complete a lid."""
        self._disarm()
        self.lidcam.view.hide_all()
        if program and self._generated:
            self._run = {"generated": self._generated, "engraver": self.session.engraver,
                         "started_at": ES.now_utc()}
            self.session.touch()
        else:
            self._run = None

    def on_run_completed(self):
        """The program ran to its end with no error: record and cross off the lid."""
        run, self._run = self._run, None
        if not run:
            return
        generated = run["generated"]
        job = generated.get("job")
        text = generated.get("text") or ""

        EngravingLog.submit(ES.completion_record(
            engraver=run["engraver"] or self.session.engraver,
            engraving_text=text,
            machine=self.machine_name(),
            lid=generated.get("lid"),
            row=job and job["row"],
            job=job,
            started_at=run["started_at"],
            completed_at=ES.now_utc(),
        ))
        if self.session.logged_in:
            self.session.count += 1
            self.session.touch()
        self._update_stats()
        # The engraved lid is still on the bed: the camera waits for it to go.
        self.lidcam.engraved()

        if not job:
            self._status(_("✓ Engraving '%s' complete.") % text)
            return

        self.progress.mark(job["tote_key"], job["row_key"])
        LanSync.broadcast_completion(job["tote_key"], job["row_key"])
        if self.active_job is job:
            self.active_job = None
        self._refresh_tree()

        tote = self.tote(job["tote_key"])
        left = len(self._remaining(tote)) if tote is not None else 0
        noun = _("Tote") if job["search_mode"] == "Tote" else _("Order")
        if left:
            self._status(_("✓ '%s' engraved - %d left in %s %s.") % (text, left, noun, job["scan"]))
        else:
            self._status(_("✓ '%s' engraved - %s %s complete.") % (text, noun, job["scan"]))
        # The next lid, in this tote while it has any. Focus goes to the list,
        # which also takes a barcode scan for the next tote.
        self._select_next(tote)

    # ============================================================ settings
    def show_settings_dialog(self):
        dialog = Toplevel(self)
        dialog.title(_("Engraving Station Settings"))
        dialog.transient(self)
        dialog.withdraw()
        f = Frame(dialog, padx=15, pady=15)
        f.pack(fill=BOTH, expand=YES)

        machine = StringVar(value=Utils.getStr(_SECTION, "machineName", ""))
        engravers = StringVar(value=", ".join(self.roster()))
        idle = StringVar(value="%g" % (self.idle_seconds() / 60))

        Label(f, text=_("Machine Name:")).grid(row=0, column=0, sticky="e", padx=(0, 5), pady=4)
        Entry(f, textvariable=machine, width=40).grid(row=0, column=1, sticky=W, pady=4)
        Label(f, text=_("Blank uses this computer's name: %s") % socket.gethostname(),
              font=("", 8, "italic"), fg="gray").grid(row=1, column=1, sticky=W)
        Label(f, text=_("Engravers:")).grid(row=2, column=0, sticky="e", padx=(0, 5), pady=4)
        Entry(f, textvariable=engravers, width=40).grid(row=2, column=1, sticky=W, pady=4)
        Label(f, text=_("Comma separated - one login button each."),
              font=("", 8, "italic"), fg="gray").grid(row=3, column=1, sticky=W)
        Label(f, text=_("Idle Logout (min):")).grid(row=4, column=0, sticky="e", padx=(0, 5), pady=4)
        Entry(f, textvariable=idle, width=8).grid(row=4, column=1, sticky=W, pady=4)

        msg = StringVar()
        Label(f, textvariable=msg, fg="red").grid(row=5, column=0, columnspan=2, sticky=W)

        def save():
            try:
                minutes = float(idle.get())
                if minutes <= 0:
                    raise ValueError
            except ValueError:
                msg.set(_("Idle logout must be a number of minutes above 0."))
                return
            Utils.addSection(_SECTION)
            Utils.setStr(_SECTION, "machineName", machine.get().strip())
            Utils.setStr(_SECTION, "engravers", ",".join(ES.parse_roster(engravers.get())))
            Utils.setStr(_SECTION, "idleMinutes", "%g" % minutes)
            dialog.destroy()
            if not self.session.logged_in:
                self._render_names()
            self._update_stats()

        bf = Frame(f)
        bf.grid(row=6, column=0, columnspan=2, sticky="e", pady=(10, 0))
        Button(bf, text=_("Save"), width=12, command=save).pack(side=LEFT, padx=5)
        Button(bf, text=_("Cancel"), width=12, command=dialog.destroy).pack(side=LEFT)

        self._gen()._fit_and_center(dialog)
        dialog.deiconify()
        dialog.grab_set()
        self.wait_window(dialog)
