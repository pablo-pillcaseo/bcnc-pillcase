"""Tote Scanning: engraver login and the scanned tote's list of lids.

This frame is the Tote Scanning section of the Lid Engravings tab (SurfAlignPage).
The loop at the machine:
  1. The engraver logs in here - tap a name, or scan / type it - as at the logger's
     station tablet. A scan with nobody logged in lands here first.
  2. A tote/order scan is looked up in ShipHero and its lids are listed here.
  3. Engrave Selected fills the G-code fields and generates the preview. The
     engraver checks it and presses Align & Run.
  4. When that program finishes without error the lid is crossed off, a completion
     record is handed to EngravingLog, and the next lid is selected - or, once the
     tote is done, the scan field is ready for the next tote.
"""

import socket
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
import PillcaseOrder
import Utils
from Utils import _

_SECTION = "Engraving"


class EngravingFrame(CNCRibbon.PageFrame):

    def __init__(self, master, app):
        CNCRibbon.PageFrame.__init__(self, master, "Engraving", app)
        self.app.engraving = self

        self.session = ES.Session()
        self.progress = ES.ProgressStore(Utils.iniUser + "-engraving-progress.json")
        LanSync.start(self.progress, self.machine_name())

        # The scanned tote/order on show
        self.rows = []
        self.row_keys = []
        self.scan_value = ""
        self.search_mode = "Order"
        self.tote_key = None
        self.lid_override = {}      # row index -> lid picked by hand

        self.active_job = None      # the lid last sent to SurfAlign from this list
        self._generated = None      # what the loaded G-code was generated from
        self._run = None            # snapshot of the program run in progress
        self._pending_scan = None   # scanned before anyone logged in
        self._icon_cache = {}

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

        self.tote_var = StringVar(value=_("Scan a tote to list its engravings."))
        self.tote_lbl = Label(box, textvariable=self.tote_var, font=("", 11, "bold"),
                              anchor=W, justify=LEFT, wraplength=300)
        self.tote_lbl.pack(side=TOP, fill=X)

        # Above the list, so it stays on screen however short the window is: Tk
        # squeezes the last-packed widgets (the details) first.
        self.engrave_btn = Button(box, text=_("Engrave Selected  ▶"), command=self.engrave_selected,
                                  bg="#2E75B6", fg="white", activebackground="#1F3B57",
                                  activeforeground="white", font=("", 11, "bold"))
        self.engrave_btn.pack(side=TOP, fill=X, pady=(4, 0))
        self.addWidget(self.engrave_btn)
        self.warn_var = StringVar()
        warn_lbl = Label(box, textvariable=self.warn_var, fg="red", anchor=W, justify=LEFT,
                         wraplength=300)
        # Takes a line only while there is a warning to show
        self.warn_var.trace_add("write", lambda *a: warn_lbl.pack(
            side=TOP, fill=X, after=self.engrave_btn) if self.warn_var.get() else warn_lbl.pack_forget())

        style = ttk.Style()
        style.configure("EngravingItems.Treeview", rowheight=24)
        style.layout("EngravingItems.Treeview.Item", [
            ("Treeitem.padding", {"sticky": "nswe", "children": [
                ("Treeitem.image", {"side": "left", "sticky": ""}),
                ("Treeitem.text", {"sticky": "nswe"}),
            ]})
        ])
        tc = Frame(box)
        # Fixed and short, so the preview controls and Align & Run below Tote
        # Scanning stay on screen; a bigger tote scrolls the list.
        tc.pack(side=TOP, fill=X, pady=2)
        tree = self.tree = ttk.Treeview(tc, style="EngravingItems.Treeview",
                                        columns=("Engraving", "Order"),
                                        show="tree headings", height=5)
        tree.heading("#0", text=_("Case Type / Colour"))
        tree.column("#0", width=160, minwidth=120)
        tree.heading("Engraving", text=_("Engraving"))
        tree.column("Engraving", width=80, minwidth=50)
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
        tree.tag_configure("done", foreground="gray", font=done_font)
        tree.tag_configure("unmapped", background="#ffcccc")
        tree.tag_configure("warned", background="#fff0cc")

        tree.bind("<<TreeviewSelect>>", self._on_select)
        tree.bind("<Double-1>", self.engrave_selected)
        tree.bind("<Return>", self.engrave_selected)

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
        """Show a looked-up tote/order, ready to pick a lid."""
        self.scan_value = str(scan_value or "").strip()
        self.search_mode = search_mode
        self.tote_key = ES.scan_key(search_mode, self.scan_value)
        self.rows = list(rows)
        self.row_keys = ES.row_keys(self.rows)
        self.lid_override = {}
        self.session.touch()

        self.scan_entry.delete(0, END)
        self.scan_entry.insert(0, self.scan_value)
        self.warn_var.set("")
        self._refresh_tree()
        self._show()
        self._select_next()

    # ================================================================ list
    def _completed(self):
        return self.progress.completed(self.tote_key) if self.tote_key else set()

    def _remaining(self):
        done = self._completed()
        return [i for i, row in enumerate(self.rows)
                if ES.has_engraving(row) and self.row_keys[i] not in done]

    def _lid_for(self, i):
        if i in self.lid_override:
            return self.lid_override[i]
        gen = self._gen()
        if not hasattr(gen, "_lid_defaults"):
            gen._lid_defaults = gen._load_lid_defaults()
        hits = PillcaseOrder.lids_for_product(gen._lid_defaults, self.rows[i].get("product_code"))
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

    def _header_text(self):
        noun = _("Tote") if self.search_mode == "Tote" else _("Order")
        total = sum(1 for r in self.rows if ES.has_engraving(r))
        left = len(self._remaining())
        if not total:
            return _("%s %s - nothing to engrave") % (noun, self.scan_value)
        if not left:
            return _("✓ %s %s - COMPLETE, all %d engraved") % (noun, self.scan_value, total)
        return _("%s %s - %d of %d engraved") % (noun, self.scan_value, total - left, total)

    def _refresh_tree(self):
        tree = self.tree
        selected = tree.selection()
        tree.delete(*tree.get_children())
        done = self._completed()
        for i, row in enumerate(self.rows):
            engraving = ES.has_engraving(row)
            if self.hide_non_engraving.get() and not engraving:
                continue
            finished = engraving and self.row_keys[i] in done
            if finished:
                tags = ("done",)
            elif not self._lid_for(i):
                tags = ("unmapped",)
            elif row.get("warnings"):
                tags = ("warned",)
            else:
                tags = ()
            case = row.get("case_type") or row.get("product_code") or _("Unknown case")
            text = "%s  –  %s" % (case, row.get("colour_name") or _("colour?"))
            if finished:
                text = "✓ " + text
            icon = self._colour_icon(row)
            kw = {"image": icon, "text": "  " + text} if icon else {"text": text}
            tree.insert("", END, iid=str(i), values=(row.get("engraving") or "",
                                                     row.get("order_number") or ""),
                        tags=tags, **kw)
        if self.rows:
            self.tote_var.set(self._header_text())
            self.tote_lbl.config(fg="#1a7f37" if not self._remaining() else "black")
        for iid in selected:
            if tree.exists(iid):
                tree.selection_set(iid)

    def _select_next(self):
        remaining = set(self._remaining())
        children = self.tree.get_children()
        target = next((iid for iid in children if int(iid) in remaining), None)
        if target is None and children:
            target = children[0]
        if target is None:
            self._on_select()
            return
        self.tree.selection_set(target)
        self.tree.focus(target)
        self.tree.see(target)
        self.tree.focus_set()

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return None, None
        i = int(sel[0])
        return i, self.rows[i]

    def _on_select(self, event=None):
        self.warn_var.set("")
        i, row = self._selected()
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
        self.d_lid.set(self._lid_for(i) or "")
        self.d_source.set("%s  |  %s  |  %s" % (row.get("product_name") or "",
                                                row.get("sku") or _("no SKU"), row.get("rule") or ""))
        self.d_warnings.set("\n".join(row.get("warnings") or []))

    def _on_lid_override(self, event=None):
        i, row = self._selected()
        if row is None:
            return
        lid = self.d_lid.get()
        if lid:
            self.lid_override[i] = lid
            self._refresh_tree()

    # ============================================================= engrave
    def engrave_selected(self, event=None):
        """Fill the G-code fields from the selected lid and generate its preview."""
        i, row = self._selected()
        if row is None:
            self._warn(_("Select a case first."))
            return "break"
        page = self._page()
        if self.app.running or (page is not None and page.busy()):
            self._warn(_("The machine is running - wait for it to finish."))
            return "break"
        if not ES.has_engraving(row):
            self._warn(_("This item has no Lid Engraving text."))
            return "break"
        lid = self._lid_for(i)
        if not lid:
            self._warn(_("This case type is not mapped to any Lid. Pick one from the Lid dropdown."))
            return "break"
        key = self.row_keys[i]
        if key in self._completed() and not messagebox.askyesno(
                _("Already Engraved"),
                _("This lid is already marked as engraved.\n\nEngrave it again?"), parent=self):
            return "break"

        text = str(row.get("engraving"))
        gen = self._gen()
        gen.engraveText.set(text)
        gen.lidName.set(lid)
        gen._apply_defaults_to_main_fields_if_available(lid)

        self.active_job = {
            "tote_key": self.tote_key, "row_key": key, "row": dict(row), "lid": lid,
            "scan": self.scan_value, "search_mode": self.search_mode,
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
        else:
            if ok:
                self._status(_("Ready - check the preview for '%s' on %s, then press Align & Run.")
                             % (text, lid))
                if page is not None:
                    page.show_run()
            else:
                self._status(_("GCode generation failed - fields imported, not generated."), error=True)
        return "break"

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

        if not job:
            self._status(_("✓ Engraving '%s' complete.") % text)
            return

        self.progress.mark(job["tote_key"], job["row_key"])
        LanSync.broadcast_completion(job["tote_key"], job["row_key"])
        if self.active_job is job:
            self.active_job = None
        if job["tote_key"] == self.tote_key:
            self._refresh_tree()

        left = len(self._remaining())
        if left:
            self._show()
            self._select_next()
            self._status(_("✓ '%s' engraved - %d left. Pick the next case.") % (text, left))
        else:
            noun = _("Tote") if job["search_mode"] == "Tote" else _("Order")
            self._status(_("✓ '%s' engraved - %s %s complete. Scan the next one.")
                         % (text, noun, job["scan"]))
            self.focus_scan()

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
