# $Id$
#
# Author: Vasilis Vlachoudis
#  Email: vvlachoudis@gmail.com
#   Date: 18-Jun-2015

# import time
from Utils import _
import math
import sys
import time
import winreg
import re
from tkinter import (
    YES,
    N,
    S,
    W,
    E,
    NW,
    SW,
    NE,
    SE,
    EW,
    NSEW,
    CENTER,
    NONE,
    X,
    Y,
    BOTH,
    LEFT,
    TOP,
    RIGHT,
    BOTTOM,
    HORIZONTAL,
    END,
    NORMAL,
    DISABLED,
    Entry,
    StringVar,
    IntVar,
    BooleanVar,
    Button,
    Checkbutton,
    Label,
    Scale,
    Spinbox,
    LabelFrame,
    messagebox,
    Radiobutton,
    Toplevel,
    Frame,
    PanedWindow,
    VERTICAL,
    Scrollbar,
)

import Camera
import CNCRibbon
import PillcaseOrder
import Ribbon
import tkExtra
import Utils
from CNC import CNC, Block
import os
from SurfAlignUtils import setup_blender_scene
from Helpers import N_
import tkinter.font as tkFont 
from tkinter import ttk
import threading
from tkinter import Tk, font
import tkinter
import tkinter as tk
from fontTools.ttLib import TTFont
import requests
import keyring


__author__ = Utils.__author__
__email__ = Utils.__email__

PROBE_CMD = [
    _("G38.2 stop on contact else error"),
    _("G38.3 stop on contact"),
    _("G38.4 stop on loss contact else error"),
    _("G38.5 stop on loss contact"),
]

TOOL_POLICY = [
    _("Send M6 commands"),  # 0
    _("Ignore M6 commands"),  # 1
    _("Manual Tool Change (WCS)"),  # 2
    _("Manual Tool Change (TLO)"),  # 3
    _("Manual Tool Change (NoProbe)"),  # 4
]

TOOL_WAIT = [_("ONLY before probing"), _("BEFORE & AFTER probing")]

CAMERA_LOCATION = {
    "Gantry": NONE,
    "Top-Left": NW,
    "Top": N,
    "Top-Right": NE,
    "Left": W,
    "Center": CENTER,
    "Right": E,
    "Bottom-Left": SW,
    "Bottom": S,
    "Bottom-Right": SE,
}
CAMERA_LOCATION_ORDER = [
    "Gantry",
    "Top-Left",
    "Top",
    "Top-Right",
    "Left",
    "Center",
    "Right",
    "Bottom-Left",
    "Bottom",
    "Bottom-Right",
]

FONT_SIZE_STEP = 0.5            # Font Size -/+ buttons
NUDGE_STEPS = ("0.1", "0.5", "1")  # mm per nudge arrow press
PREVIEW_DELAY_MS = 700          # quiet time after an adjustment before regenerating


# =============================================================================
# Probe Tab Group
# =============================================================================
class ProbeTabGroup(CNCRibbon.ButtonGroup):
    def __init__(self, master, app):
        CNCRibbon.ButtonGroup.__init__(self, master, N_("SurfAlign"), app)

        self.tab = StringVar()
        # ---
        col, row = 0, 0
        b = Ribbon.LabelRadiobutton(
            self.frame,
            image=Utils.icons["probe32"],
            text=_("Probe"),
            compound=TOP,
            variable=self.tab,
            value="Probe",
            background=Ribbon._BACKGROUND,
        )
        b.grid(row=row, column=col, padx=5, pady=0, sticky=NSEW)
        tkExtra.Balloon.set(b, _("Simple probing along a direction"))

        # ---
        col += 1
        b = Ribbon.LabelRadiobutton(
            self.frame,
            image=Utils.icons["level32"],
            text=_("Autolevel"),
            compound=TOP,
            variable=self.tab,
            value="Autolevel",
            background=Ribbon._BACKGROUND,
        )
        b.grid(row=row, column=col, padx=5, pady=0, sticky=NSEW)
        tkExtra.Balloon.set(b, _("Autolevel Z surface"))

        # ---
        col += 1
        b = Ribbon.LabelRadiobutton(
            self.frame,
            image=Utils.icons["camera32"],
            text=_("Camera"),
            compound=TOP,
            variable=self.tab,
            value="Camera",
            background=Ribbon._BACKGROUND,
        )
        b.grid(row=row, column=col, padx=5, pady=0, sticky=NSEW)
        tkExtra.Balloon.set(b, _("Work surface camera view and alignment"))
        if Camera.cv is None:
            b.config(state=DISABLED)

        # ---
        col += 1
        b = Ribbon.LabelRadiobutton(
            self.frame,
            image=Utils.icons["endmill32"],
            text=_("Tool"),
            compound=TOP,
            variable=self.tab,
            value="Tool",
            background=Ribbon._BACKGROUND,
        )
        b.grid(row=row, column=col, padx=5, pady=0, sticky=NSEW)
        tkExtra.Balloon.set(b, _("Setup probing for manual tool change"))

        self.frame.grid_rowconfigure(0, weight=1)


# =============================================================================
# Probe Common Offset
# =============================================================================
class ProbeCommonFrame(CNCRibbon.PageFrame):
    probeFeed = None
    tlo = None
    probeCmd = None

    def __init__(self, master, app):
        CNCRibbon.PageFrame.__init__(self, master, "ProbeCommon1", app)

        lframe = tkExtra.ExLabelFrame(
            self, text=_("Common"), foreground="DarkBlue")
        lframe.pack(side=TOP, fill=X)
        frame = lframe.frame

        # ----
        row = 0
        col = 0

        # ----
        # Fast Probe Feed
        Label(frame,
              text=_("Fast Probe Feed:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.fastProbeFeed = StringVar()
        self.fastProbeFeed.trace(
            "w", lambda *_: ProbeCommonFrame.probeUpdate())
        ProbeCommonFrame.fastProbeFeed = tkExtra.FloatEntry(
            frame,
            background=tkExtra.GLOBAL_CONTROL_BACKGROUND,
            width=5,
            textvariable=self.fastProbeFeed,
        )
        ProbeCommonFrame.fastProbeFeed.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            ProbeCommonFrame.fastProbeFeed,
            _("Set initial probe feed rate for tool change and calibration"),
        )
        self.addWidget(ProbeCommonFrame.fastProbeFeed)

        # ----
        # Probe Feed
        row += 1
        col = 0
        Label(frame, text=_("Probe Feed:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.probeFeedVar = StringVar()
        self.probeFeedVar.trace("w", lambda *_: ProbeCommonFrame.probeUpdate())
        ProbeCommonFrame.probeFeed = tkExtra.FloatEntry(
            frame,
            background=tkExtra.GLOBAL_CONTROL_BACKGROUND,
            width=5,
            textvariable=self.probeFeedVar,
        )
        ProbeCommonFrame.probeFeed.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            ProbeCommonFrame.probeFeed, _("Set probe feed rate"))
        self.addWidget(ProbeCommonFrame.probeFeed)

        # ----
        # Tool offset
        row += 1
        col = 0
        Label(frame, text=_("TLO")).grid(row=row, column=col, sticky=E)
        col += 1
        ProbeCommonFrame.tlo = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        ProbeCommonFrame.tlo.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            ProbeCommonFrame.tlo, _("Set tool offset for probing"))
        self.addWidget(ProbeCommonFrame.tlo)
        self.tlo.bind("<Return>", self.tloSet)
        self.tlo.bind("<KP_Enter>", self.tloSet)

        col += 1
        b = Button(frame, text=_("set"), command=self.tloSet, padx=2, pady=1)
        b.grid(row=row, column=col, sticky=EW)
        self.addWidget(b)

        # ---
        # feed command
        row += 1
        col = 0
        Label(frame,
              text=_("Probe Command")).grid(row=row, column=col, sticky=E)
        col += 1
        ProbeCommonFrame.probeCmd = tkExtra.Combobox(
            frame,
            True,
            background=tkExtra.GLOBAL_CONTROL_BACKGROUND,
            width=16,
            command=ProbeCommonFrame.probeUpdate,
        )
        ProbeCommonFrame.probeCmd.grid(row=row, column=col, sticky=EW)
        ProbeCommonFrame.probeCmd.fill(PROBE_CMD)
        self.addWidget(ProbeCommonFrame.probeCmd)

        frame.grid_columnconfigure(1, weight=1)
        self.loadConfig()

    # ------------------------------------------------------------------------
    def tloSet(self, event=None):
        try:
            CNC.vars["TLO"] = float(ProbeCommonFrame.tlo.get())
            cmd = f"G43.1Z{ProbeCommonFrame.tlo.get()}"
            self.sendGCode(cmd)
        except Exception:
            pass
        self.app.mcontrol.viewParameters()

    # ------------------------------------------------------------------------
    @staticmethod
    def probeUpdate():
        try:
            CNC.vars["fastprbfeed"] = float(
                ProbeCommonFrame.fastProbeFeed.get())
            CNC.vars["prbfeed"] = float(ProbeCommonFrame.probeFeed.get())
            CNC.vars["prbcmd"] = str(
                ProbeCommonFrame.probeCmd.get().split()[0])
            return False
        except Exception:
            return True

    # ------------------------------------------------------------------------
    def updateTlo(self):
        try:
            if self.focus_get() is not ProbeCommonFrame.tlo:
                state = ProbeCommonFrame.tlo.cget("state")
                state = ProbeCommonFrame.tlo["state"] = NORMAL
                ProbeCommonFrame.tlo.set(str(CNC.vars.get("TLO", "")))
                state = ProbeCommonFrame.tlo["state"] = state
        except Exception:
            pass

    # -----------------------------------------------------------------------
    def saveConfig(self):
        Utils.setFloat("Probe",
                       "fastfeed", ProbeCommonFrame.fastProbeFeed.get())
        Utils.setFloat("Probe", "feed", ProbeCommonFrame.probeFeed.get())
        Utils.setFloat("Probe", "tlo", ProbeCommonFrame.tlo.get())
        Utils.setFloat("Probe", "cmd",
                       ProbeCommonFrame.probeCmd.get().split()[0])

    # -----------------------------------------------------------------------
    def loadConfig(self):
        ProbeCommonFrame.fastProbeFeed.set(Utils.getFloat("Probe", "fastfeed"))
        ProbeCommonFrame.probeFeed.set(Utils.getFloat("Probe", "feed"))
        ProbeCommonFrame.tlo.set(Utils.getFloat("Probe", "tlo"))
        cmd = Utils.getStr("Probe", "cmd")
        for p in PROBE_CMD:
            if p.split()[0] == cmd:
                ProbeCommonFrame.probeCmd.set(p)
                break



# =============================================================================
# Probe Common Offset
# =============================================================================
class GenGcodeFrame(CNCRibbon.PageFrame):
    probeFeed = None
    tlo = None
    probeCmd = None
    orderNumber = None

    # ===================== Lid Defaults (FontSize/Depth/LayerHeight) =====================
    def _load_lid_defaults(self):
        """Return {lid_name: {'fontSize', 'depth', 'layerHeight', 'rotation',
        'width', 'length', 'productCode'}}, each value float|str|None."""
        try:
            import json
            raw = Utils.getStr("SurfAlign", "lidDefaults")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _save_lid_defaults(self):
        try:
            import json
            Utils.setStr("SurfAlign", "lidDefaults", json.dumps(self._lid_defaults))
        except Exception:
            pass

    def _float_or_none(self, v):
        try:
            return float(v)
        except Exception:
            return None

    def _apply_defaults_to_main_fields_if_available(self, lid=None):
        """Apply saved defaults for given lid (or current) to main 3 fields."""
        if lid is None:
            lid = self.lidName.get().strip()
        if not lid:
            return
        cfg = getattr(self, "_lid_defaults", {}).get(lid, {})
        if not isinstance(cfg, dict):
            return
        if "fontSize" in cfg and cfg["fontSize"] is not None:
            self.fontSize.set(cfg["fontSize"])
        if "depth" in cfg and cfg["depth"] is not None:
            self.engraveDepth.set(cfg["depth"])
        if "layerHeight" in cfg and cfg["layerHeight"] is not None:
            self.layerHeight.set(cfg["layerHeight"])
        if "rotation" in cfg and cfg["rotation"] is not None:
            self.rotation.set(cfg["rotation"])

    def _center_window(self, win, width, height):
        """Center a Toplevel over the application window, clamped to the screen.

        The caller passes the intended size because winfo_width() still reports
        1 before the window is mapped, which pushed windows off screen.
        """
        root = self.winfo_toplevel()
        root.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - width) // 2
        y = root.winfo_rooty() + (root.winfo_height() - height) // 2
        x = max(0, min(x, win.winfo_screenwidth() - width))
        y = max(0, min(y, win.winfo_screenheight() - height))
        win.geometry(f"{width}x{height}+{x}+{y}")

    def _fit_and_center(self, win, min_width=0, min_height=0):
        """Size a Toplevel to what its widgets ask for, then centre it.

        A hard-coded geometry is a guess at the content's size, and it goes wrong
        as soon as the content grows or Windows display scaling enlarges the
        fonts - the bottom of the dialog, where Save/Cancel live, is what gets
        cut off. Call this after the widgets are built; minsize then stops the
        window from being dragged smaller than its content.
        """
        win.update_idletasks()
        width = max(win.winfo_reqwidth(), min_width)
        height = max(win.winfo_reqheight(), min_height)
        win.minsize(width, height)
        self._center_window(win, width, height)

    def _scan_status(self, msg, error=False):
        """Non-modal feedback for the scan loop: status bar text, bell on error."""
        self.app.statusbar.configText(text=msg, fill="Red" if error else "DarkBlue")
        self.app.statusbar.update_idletasks()
        if error:
            self.bell()

    def _focus_scan_field(self):
        """Return focus to the scan entry, selected, ready for the next scan."""
        engraving = getattr(self.app, "engraving", None)
        if engraving is not None:
            engraving.focus_scan()

    def _on_main_lid_changed(self, event=None):
        """When user selects a lid in the main UI, apply its defaults."""
        self._apply_defaults_to_main_fields_if_available()
        width, height = self.get_lid_dimensions()
        if width is None or height is None:
            # A lid whose size has not been filled in yet. Previously unreachable
            # (the name always carried one), now it is — so clear the outline
            # instead of raising on `-1 * None`.
            self.app.canvasFrame.canvas.drawLidOutline(None)
            return
        outline_cords=(0, 0), (width, -1 * height)
        self.app.canvasFrame.canvas.drawLidOutline(outline_cords)
    # =====================================================================================

    def get_installed_font_names(self):
        font_dir = os.path.join(os.environ['WINDIR'], 'Fonts')
        font_names = []
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
                for i in range(winreg.QueryInfoKey(key)[1]):
                    raw_name, file_name, _ = winreg.EnumValue(key, i)
                    clean_name = raw_name.split(' (')[0].strip()
                    font_names.append(clean_name)
        except Exception as e:
            print("🛑 Error reading fonts:", e)
        return sorted(set(font_names))
    
    def get_font_name_style(self, font_path):
        try:
            font = TTFont(font_path, lazy=True)
            name = ""
            subfamily = ""
            for record in font["name"].names:
                if record.nameID == 1 and not name:
                    name = record.toUnicode()
                elif record.nameID == 2 and not subfamily:
                    subfamily = record.toUnicode()
            font.close()
            return name, subfamily
        except Exception as e:
            print(f"⚠️ Failed to read {font_path}: {e}")
            return None, None


    def load_fonts_from_folder(self, folder):
        font_dict = {}
        for file in os.listdir(folder):
            if file.lower().endswith(('.ttf', '.otf')):
                font_path = os.path.join(folder, file)
                family, style = self.get_font_name_style(font_path)
                if family:
                    key = f"{family} {style}".strip()
                    font_dict[key] = font_path
                    print(f"{file} => Family: {family}, Style: {style}")
                else:
                    print(f"{file} => Unable to read font name")
                    pass
        return font_dict
    
    def load_fonts_from_registry(self):
        font_dict = {}
        fonts_dir = os.path.join(os.environ["WINDIR"], "Fonts")

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
                count = winreg.QueryInfoKey(key)[1]
                for i in range(count):
                    try:
                        name, font_file, _ = winreg.EnumValue(key, i)
                        if font_file.lower().endswith(('.ttf', '.otf')):
                            font_path = os.path.join(fonts_dir, font_file)
                            if os.path.exists(font_path):
                                family, style = self.get_font_name_style(font_path)
                                if family:
                                    key_name = f"{family} {style}".strip()
                                    if key_name not in font_dict:
                                        font_dict[key_name] = font_path
                                        # print(f"{font_file} => Family: {family}, Style: {style}")
                                    else:
                                        # print(f"{font_file} => Duplicate skipped: {family} {style}")
                                        pass
                                else:
                                    # print(f"{font_file} => Unable to read font name")
                                    pass
                    except Exception as e:
                        print(f"⚠️ Error processing registry entry: {e}")
        except Exception as e:
            print(f"⚠️ Failed to access Windows font registry: {e}")

        return font_dict

    def __init__(self, master, app, common=None):
        CNCRibbon.PageFrame.__init__(self, master, "GenGcode", app)

        self.app.surfalign_gen_gcode_frame = self
        self._preview_id = None
        self._generated_key = None

        # The few fields an engraver checks against the preview live in `common`,
        # always on show; everything else is in this frame, under Advanced Settings.
        self._build_common(common if common is not None else self)

        lframe = tkExtra.ExLabelFrame(
            self, text=_("GCode"), foreground="DarkBlue")
        lframe.pack(side=TOP, fill=X)
        frame = lframe.frame

        # ----
        row = 0
        col = 0

        # Font selection dropdown
        Label(lframe(), text=_("Font:")).grid(row=row, column=col, sticky=E)
        col += 1

        # Load fonts folders from config
        fonts_folders_str = Utils.getStr("SurfAlign", "fontsFolders")
        self.fonts_folders = [folder.strip() for folder in fonts_folders_str.split(",") if folder.strip()] if fonts_folders_str else []

        self.all_font_dict = {}
        for fonts_folder in self.fonts_folders:
            self.all_font_dict.update(self.load_fonts_from_folder(fonts_folder))

        # 2️⃣ Load system fonts from registry
        self.all_font_dict.update(self.load_fonts_from_registry())

        font_list = sorted(set(self.all_font_dict.keys()))

        self.font_var = StringVar()
        self.font_selector = ttk.Combobox(
            lframe(),
            textvariable=self.font_var,
            values=font_list,
            width=30,
            state="readonly",
        )
        self.font_selector.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(self.font_selector, _("Select font for engraving text"))
        self.addWidget(self.font_selector)

        col += 1

        # Add Font Folder button
        add_font_folder_button = Button(lframe(), text=_("Add Font Folder"), command=self.show_add_font_folder_dialog, padx=2, pady=1)
        add_font_folder_button.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(add_font_folder_button, _("Add a new font folder path"))
        self.addWidget(add_font_folder_button)

        # Add gap distance field for pipe-separated text
        row += 1
        col = 0
        Label(frame, text=_("| Sep Gap (mm):")).grid(row=row, column=col, sticky=E)
        col += 1
        self.gapDistance = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.gapDistance.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.gapDistance, _("Distance between words when using | separator in text"))
        self.addWidget(self.gapDistance)

        row += 1
        col = 0
        Label(lframe(), text=_("Text Positioning:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.textPositioning_var = StringVar()
        self.textPositioning_selector = ttk.Combobox(
            lframe(),
            textvariable=self.textPositioning_var,
            values=["Lid Center", "Direct"],
            width=30,
            state="readonly",
        )
        self.textPositioning_selector.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(self.textPositioning_selector, _("Select text positioning method"))
        self.addWidget(self.textPositioning_selector)

        # Bind the callback to show/hide lid selector
        self.textPositioning_selector.bind('<<ComboboxSelected>>', self.on_text_positioning_change)

        # ---- Lid Selector
        lid_list_str = Utils.getStr("SurfAlign", "lidList")
        self.lid_list = [lid.strip() for lid in lid_list_str.split(",") if lid.strip()] if lid_list_str and lid_list_str.strip() else []

        row += 1
        col = 0
        self.lid_label = Label(lframe(), text=_("Lid Name:"))
        self.lid_label.grid(row=row, column=col, sticky=E)
        col += 1
        self.lidName_selector = ttk.Combobox(lframe(), textvariable=self.lidName, values=self.lid_list, width=30, state="readonly")
        self.lidName_selector.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(self.lidName_selector, _("Select lid name"))
        self.addWidget(self.lidName_selector)
        # Auto-apply saved defaults for selected lid
        self.lidName_selector.bind('<<ComboboxSelected>>', self._on_main_lid_changed)

        col += 1
        self.edit_lid_button = Button(lframe(), text=_("Edit"), command=self.show_edit_lid_dialog, padx=2, pady=1)
        self.edit_lid_button.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(self.edit_lid_button, _("Edit lid names list"))
        self.addWidget(self.edit_lid_button)

        row += 1
        col = 0
        self.center_offset_label = Label(lframe(), text=_("Offset (mm):"))
        self.center_offset_label.grid(row=row, column=col, sticky=E)
        col += 1
        self.center_offset_x = tkExtra.FloatEntry(
            lframe(), background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.center_offset_x.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(self.center_offset_x, _("Text Position Offset from Lid Center X"))
        self.addWidget(self.center_offset_x)

        col += 1
        self.center_offset_y = tkExtra.FloatEntry(
            lframe(), background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.center_offset_y.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(self.center_offset_y, _("Text Position Offset from Lid Center Y"))
        self.addWidget(self.center_offset_y)

        # Initially hide the lid selector (will be shown when "Lid Center" is selected)
        self.lid_label.grid_remove()
        self.lidName_selector.grid_remove()
        self.edit_lid_button.grid_remove()
        self.center_offset_label.grid_remove()
        self.center_offset_x.grid_remove()
        self.center_offset_y.grid_remove()

        # ----
        # Pos (X, Y)
        row, col = row + 1, 0
        self.pos_label = Label(lframe(), text=_("Center Pos:"))
        self.pos_label.grid(row=row, column=col, sticky=E)

        col += 1
        self.posX = tkExtra.FloatEntry(
            lframe(), background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.posX.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(self.posX, _("Engrave Text Center Position X"))
        self.addWidget(self.posX)

        col += 1
        self.posY = tkExtra.FloatEntry(
            lframe(), background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.posY.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(self.posY, _("Engrave Text Center Position Y"))
        self.addWidget(self.posY)

        self.pos_label.grid_remove()
        self.posX.grid_remove()
        self.posY.grid_remove()

        # ----
        # Nudge arrows: move the text in X/Y by a step, then refresh the preview
        row += 1
        Label(frame, text=_("Nudge Text:")).grid(row=row, column=0, sticky=NE, pady=(4, 0))
        self._build_nudge(frame).grid(row=row, column=1, columnspan=2, sticky=W, pady=(4, 0))

        # ----
        # Rotation
        row += 1
        col = 0
        Label(frame, text=_("Rotation:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.rotation = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.rotation.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.rotation, _("Engrave Text Rotation along Z axis (degrees)"))
        self.addWidget(self.rotation)

        # ----
        # Feedrate
        row += 1
        col = 0
        Label(frame, text=_("Feedrate:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.feedrate = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.feedrate.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.feedrate, _("Feedrate (mm/min)"))
        self.addWidget(self.feedrate)

        # ----
        # Spindle RPM
        row += 1
        col = 0
        Label(frame, text=_("Spindle RPM:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.spindleRPM = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.spindleRPM.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.spindleRPM, _("Spindle RPM"))
        self.addWidget(self.spindleRPM)

        # ----
        # Engrave Depth
        row += 1
        col = 0
        Label(frame, text=_("Depth:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.engraveDepth = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.engraveDepth.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.engraveDepth, _("Engrave Depth (mm)"))
        self.addWidget(self.engraveDepth)

        # ----
        # Layer Height
        row += 1
        col = 0
        Label(frame, text=_("Layer Height:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.layerHeight = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.layerHeight.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.layerHeight, _("Layer Height"))
        self.addWidget(self.layerHeight)


        # ----
        # Safe Height
        row += 1
        col = 0
        Label(frame, text=_("Safe Height:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.safeHeight = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.safeHeight.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.safeHeight, _("Safe Height"))
        self.addWidget(self.safeHeight)

        # ----
        # Final Height
        row += 1
        col = 0
        Label(frame, text=_("Final Height:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.finalHeight = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.finalHeight.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.finalHeight, _("Height to move to after engraving is complete"))
        self.addWidget(self.finalHeight)

        col += 1
        generate_b = Button(frame, text=_("Generate"), command=self.generateGcode, padx=2, pady=1)
        generate_b.grid(row=row, column=col, sticky=EW)
        self.addWidget(generate_b)




        frame.grid_columnconfigure(1, weight=1)
        self.loadConfig()

    # ===================== Preview controls (always visible) ======================
    def _build_common(self, parent):
        """Engraving text, lid, font size and vertical centering: what an engraver
        checks against the preview before pressing Align & Run."""
        parent.grid_columnconfigure(1, weight=1)
        row = 0

        Label(parent, text=_("Engraving:")).grid(row=row, column=0, sticky=E, pady=2)
        self.engraveText = StringVar()
        entry = Entry(parent, background=tkExtra.GLOBAL_CONTROL_BACKGROUND,
                      font=("", 12), width=5, textvariable=self.engraveText)
        entry.grid(row=row, column=1, sticky=EW, pady=2)
        entry.bind('<Return>', lambda event: self.schedule_preview(0))
        entry.bind('<FocusOut>', lambda event: self.schedule_preview())
        tkExtra.Balloon.set(
            entry,
            _("Text to engrave. Use | to separate words with spacing. Use <|> for literal pipe character."),
        )
        self.addWidget(entry)

        info_button = Button(parent, text="ℹ", font=("TkDefaultFont", 8, "bold"),
                             command=self.show_text_syntax_help, width=2, height=1)
        info_button.grid(row=row, column=2, sticky=W, padx=(2, 0))
        tkExtra.Balloon.set(info_button, _("Click for detailed text syntax help"))

        row += 1
        Label(parent, text=_("Lid:")).grid(row=row, column=0, sticky=E, pady=2)
        # The lid picker itself is in Advanced Settings; the list picks the lid.
        self.lidName = StringVar()
        self.lid_display = StringVar()
        self.lidName.trace_add("write", lambda *a: self._update_lid_display())
        Label(parent, textvariable=self.lid_display, anchor=W, font=("", 10, "bold")).grid(
            row=row, column=1, columnspan=2, sticky=EW, pady=2)

        row += 1
        Label(parent, text=_("Font Size:")).grid(row=row, column=0, sticky=E, pady=2)
        size_f = Frame(parent)
        size_f.grid(row=row, column=1, columnspan=2, sticky=W, pady=2)
        b = Button(size_f, text="−", width=3, font=("", 11, "bold"),
                   command=lambda: self._step_font_size(-FONT_SIZE_STEP))
        b.pack(side=LEFT)
        self.addWidget(b)
        self.fontSize = tkExtra.FloatEntry(
            size_f, background=tkExtra.GLOBAL_CONTROL_BACKGROUND,
            font=("", 12), width=6, justify=CENTER)
        self.fontSize.pack(side=LEFT, padx=4, fill=Y)
        self.fontSize.bind('<Return>', lambda event: self.schedule_preview(0))
        self.fontSize.bind('<FocusOut>', lambda event: self.schedule_preview())
        tkExtra.Balloon.set(self.fontSize, _("Engrave Text Size"))
        self.addWidget(self.fontSize)
        b = Button(size_f, text="+", width=3, font=("", 11, "bold"),
                   command=lambda: self._step_font_size(FONT_SIZE_STEP))
        b.pack(side=LEFT)
        self.addWidget(b)

        row += 1
        Label(parent, text=_("Vertical\nCentering:"), justify=RIGHT).grid(row=row, column=0, sticky=E)
        slider_f = Frame(parent)
        slider_f.grid(row=row, column=1, columnspan=2, sticky=EW)
        Label(slider_f, text=_("box"), font=("TkDefaultFont", 8, "italic"),
              fg="gray").pack(side=LEFT, anchor=S, pady=(0, 4))
        Label(slider_f, text=_("mass"), font=("TkDefaultFont", 8, "italic"),
              fg="gray").pack(side=RIGHT, anchor=S, pady=(0, 4))
        self.yAdjustFactor = Scale(
            slider_f,
            from_=0,
            to=1,
            resolution=0.01,
            orient=HORIZONTAL,
            background=tkExtra.GLOBAL_CONTROL_BACKGROUND,
            command=lambda value: self.schedule_preview(),
        )
        self.yAdjustFactor.pack(side=LEFT, fill=X, expand=YES, padx=2)
        tkExtra.Balloon.set(
            self.yAdjustFactor,
            _("Controls how much the Y origin is influenced by the center of mass.\n"
              "0.0 → use bounding box center only (box)\n"
              "1.0 → use full center of mass (mass)\n"
              "Values in between blend smoothly between both.")
        )
        self.addWidget(self.yAdjustFactor)

    def _update_lid_display(self):
        if getattr(self, "textPositioning_var", None) is not None \
                and self.textPositioning_var.get() == "Direct":
            self.lid_display.set(_("(Direct position)"))
        else:
            self.lid_display.set(self.lidName.get() or _("No lid selected"))

    def _step_font_size(self, delta):
        try:
            size = float(self.fontSize.get())
        except ValueError:
            return
        self.fontSize.set("%g" % max(FONT_SIZE_STEP, round(size + delta, 2)))
        self.schedule_preview()

    def _build_nudge(self, parent):
        """Arrow pad that moves the text by the chosen step and refreshes the preview."""
        pad = Frame(parent)
        arrows = Frame(pad)
        arrows.pack(side=LEFT)
        for text, r, c, dx, dy, tip in (
                ("▲", 0, 1, 0, 1, _("Move text up (+Y)")),
                ("◀", 1, 0, -1, 0, _("Move text left (-X)")),
                ("▶", 1, 2, 1, 0, _("Move text right (+X)")),
                ("▼", 2, 1, 0, -1, _("Move text down (-Y)"))):
            b = Button(arrows, text=text, width=3, command=lambda dx=dx, dy=dy: self.nudge(dx, dy))
            b.grid(row=r, column=c, sticky=NSEW, padx=1, pady=1)
            tkExtra.Balloon.set(b, tip)
            self.addWidget(b)
        self.nudge_center_btn = Button(arrows, text="◎", width=3, command=self.nudge_reset)
        self.nudge_center_btn.grid(row=1, column=1, sticky=NSEW, padx=1, pady=1)
        tkExtra.Balloon.set(self.nudge_center_btn, _("Reset the offset to the lid center"))
        self.addWidget(self.nudge_center_btn)

        step_f = Frame(pad)
        step_f.pack(side=LEFT, padx=(10, 0))
        Label(step_f, text=_("Step (mm):")).pack(anchor=W)
        self.nudge_step = StringVar(value=NUDGE_STEPS[0])
        for value in NUDGE_STEPS:
            Radiobutton(step_f, text=value, value=value, variable=self.nudge_step).pack(anchor=W)
        return pad

    def _nudge_fields(self):
        """The X/Y entries a nudge moves for the current positioning mode."""
        if self.textPositioning_var.get() == "Direct":
            return self.posX, self.posY
        return self.center_offset_x, self.center_offset_y

    def nudge(self, dx, dy):
        try:
            step = float(self.nudge_step.get())
        except ValueError:
            return
        for field, d in zip(self._nudge_fields(), (dx, dy)):
            if not d:
                continue
            try:
                value = float(field.get() or 0)
            except ValueError:
                value = 0.0
            field.set("%g" % round(value + d * step, 3))
        self.schedule_preview()

    def nudge_reset(self):
        if self.textPositioning_var.get() == "Direct":
            return
        self.center_offset_x.set("0")
        self.center_offset_y.set("0")
        self.schedule_preview()

    # ============================== Preview refresh ===============================
    def _preview_key(self):
        """The adjustable inputs a preview was generated from."""
        fields = (self.engraveText, self.fontSize, self.yAdjustFactor, self.textPositioning_var,
                  self.center_offset_x, self.center_offset_y, self.posX, self.posY)
        return tuple(str(f.get()) for f in fields)

    def schedule_preview(self, delay=PREVIEW_DELAY_MS):
        """Regenerate the preview once the operator stops adjusting.

        Generating runs Blender, so a slider drag or a run of nudges is collapsed
        into one regeneration after the last change.
        """
        if self._preview_id is not None:
            self.after_cancel(self._preview_id)
        self._preview_id = self.after(delay, self._refresh_preview)

    def _refresh_preview(self):
        self._preview_id = None
        engraving = getattr(self.app, "engraving", None)
        # Only an engraving preview already on screen is refreshed: before the
        # first Engrave/Generate there is nothing to update, and a file loaded by
        # hand is not ours to replace.
        if engraving is None or engraving._generated is None:
            return
        if self._preview_key() == self._generated_key:
            return
        page = getattr(self.app, "lid_engravings", None)
        if page is not None and page.busy():
            # Loading G-code now would replace the program being probed or run.
            self._scan_status(_("Machine busy - the change applies to the next Align & Run."))
            return
        self.generateGcode()
        
    def loadConfig(self):
        # Load all lids' defaults first
        self._lid_defaults = self._load_lid_defaults()

        self.engraveText.set(Utils.getStr("SurfAlign", "engraveText"))
        self.font_var.set(Utils.getStr("SurfAlign", "textFont"))
        self.fontSize.set(Utils.getFloat("SurfAlign", "fontSize"))
        self.posX.set(Utils.getFloat("SurfAlign", "posX"))
        self.posY.set(Utils.getFloat("SurfAlign", "posY"))
        self.textPositioning_var.set(Utils.getStr("SurfAlign", "textPositioningMode"))
        self.lidName.set(Utils.getStr("SurfAlign", "lidName"))
        self.center_offset_x.set(Utils.getFloat("SurfAlign", "centerOffsetX"))
        self.center_offset_y.set(Utils.getFloat("SurfAlign", "centerOffsetY"))
        self.yAdjustFactor.set(Utils.getFloat("SurfAlign", "yAdjustFactor"))
        self.rotation.set(Utils.getFloat("SurfAlign", "rotation"))
        self.feedrate.set(Utils.getFloat("SurfAlign", "feedrate"))
        self.spindleRPM.set(Utils.getFloat("SurfAlign", "spindleRPM"))
        self.engraveDepth.set(Utils.getFloat("SurfAlign", "engraveDepth"))
        self.layerHeight.set(Utils.getFloat("SurfAlign", "layerHeight"))
        self.safeHeight.set(Utils.getFloat("SurfAlign", "safeHeight"))
        self.finalHeight.set(Utils.getFloat("SurfAlign", "finalHeight"))
        self.gapDistance.set(Utils.getFloat("SurfAlign", "gapDistance"))
        
        self.on_text_positioning_change(None)

        # If a lid is already selected, apply its saved defaults to the three fields
        if self.lidName.get().strip():
            self._apply_defaults_to_main_fields_if_available(self.lidName.get().strip())

    def get_shiphero_credentials(self):
        endpoint = Utils.getStr("SurfAlign", "shipheroEndpoint")
        num_parts_str = Utils.getStr("SurfAlign", "shipheroTokenParts")
        token = ""
        if num_parts_str and num_parts_str.isdigit():
            num_parts = int(num_parts_str)
            for i in range(num_parts):
                chunk = keyring.get_password("bCNC", f"shipheroToken_{i}")
                if chunk:
                    token += chunk
        else:
            try:
                token = keyring.get_password("bCNC", "shipheroToken") or ""
            except Exception:
                token = ""
        return endpoint, token

    def set_shiphero_credentials(self, endpoint, token):
        Utils.addSection("SurfAlign")
        Utils.setStr("SurfAlign", "shipheroEndpoint", endpoint)
        if token:
            token = token.strip()
            if token.lower().startswith("bearer "):
                token = token[len("bearer "):].strip()
            
            try:
                keyring.delete_password("bCNC", "shipheroToken")
            except Exception:
                pass
            
            chunk_size = 1000
            chunks = [token[i:i+chunk_size] for i in range(0, len(token), chunk_size)]
            
            for i in range(20):
                try:
                    keyring.delete_password("bCNC", f"shipheroToken_{i}")
                except Exception:
                    pass
            
            for i, chunk in enumerate(chunks):
                keyring.set_password("bCNC", f"shipheroToken_{i}", chunk)
            
            Utils.setStr("SurfAlign", "shipheroTokenParts", str(len(chunks)))
            
        try:
            Utils.cleanConfiguration()
            with open(Utils.iniUser, "w") as f:
                Utils.config.write(f)
        except Exception:
            pass

    def fetchShipHeroOrder(self, scanned=""):
        """Look up a scanned order/tote in ShipHero and list its lids."""
        order_number = str(scanned or "").strip()
        if not order_number:
            self._scan_status(_("Scan or type a number first."), error=True)
            self._focus_scan_field()
            return

        # Engravings are credited to whoever is logged in, so nobody engraves a
        # tote anonymously. The scan is kept and loads right after login.
        engraving = getattr(self.app, "engraving", None)
        if engraving is not None and not engraving.session.logged_in:
            engraving.require_login(order_number)
            return
        scanned = order_number

        endpoint, token = self.get_shiphero_credentials()
        if not endpoint or not token:
            if not self.show_shiphero_and_asset_config_dialog():
                return
            endpoint, token = self.get_shiphero_credentials()

        if not endpoint or not token:
            self._scan_status(_("ShipHero credentials not found - open Settings."), error=True)
            self._focus_scan_field()
            return

        searchMode = Utils.getStr("SurfAlign", "shipheroSearchMode", "Order")
        
        if searchMode == "Order":
            # Prepare order number (add # if missing)
            if not order_number.startswith("#"):
                order_number = "#" + order_number
                
            # ShipHero prices a query by what it COULD return, and a connection
            # with no `first` is priced at 100 nodes: 100 orders x 20 line items
            # cost 2101 credits per scan. Only edges[0] is ever read, so ask for
            # one order and the same lookup costs about 22. The paging argument
            # belongs on `data` (the connection), not on `orders` itself.
            query = """
            query GetOrder($orderNumber: String!) {
              orders(order_number: $orderNumber) {
                complexity
                data(first: 1) {
                  edges {
                    node {
                      order_number
                      line_items(first: 20) {
                        edges {
                          node {
                            sku
                            product_name
                            custom_options
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """
            variables = {"orderNumber": order_number}
        else:
            # Tote Mode: Totes don't usually use a # prefix
            if order_number.startswith("#"):
                order_number = order_number[1:]
                
            # Same pricing trap as the order query: without `first`, 100 totes are
            # priced in. Only edges[0] is read.
            query = """
            query GetToteOrders($toteId: String!) {
              totes(search: $toteId) {
                complexity
                data(first: 1) {
                  edges {
                    node {
                      orders {
                        order_number
                        line_items(first: 20) {
                          edges {
                            node {
                              sku
                              product_name
                              custom_options
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """
            variables = {"toteId": order_number}

        # (message, is_error) reported once the finally block has cleaned up, so
        # the progress message below never overwrites the outcome.
        final_status = None

        try:
            # Show a simple progress message or wait cursor
            self.app.setStatus(_("Fetching from ShipHero..."), True)
            self.app.config(cursor="watch")
            self.app.update()
            
            response = requests.post(
                endpoint,
                json={"query": query, "variables": variables},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10
            )
            # ShipHero answers a rejected query with HTTP 400 AND a GraphQL
            # `errors` body. Checking the status first reported only "BAD
            # REQUEST" and threw away the reason, so read the body before it.
            try:
                data = response.json()
            except ValueError:
                response.raise_for_status()
                raise
            if isinstance(data, dict) and data.get("errors"):
                error_msg = "; ".join([e.get("message", "Unknown error") for e in data["errors"]])
                final_status = (_("ShipHero error: ") + error_msg, True)
                return
            response.raise_for_status()

            # (order_number, line item edge) - a tote can hold several orders, and
            # each lid has to say which one it belongs to.
            line_items = []
            if searchMode == "Order":
                edges = data.get("data", {}).get("orders", {}).get("data", {}).get("edges", [])
                if not edges:
                    final_status = (_("No order found with number: ") + order_number, True)
                    return
                order_node = edges[0]["node"]
                line_items = [(order_node.get("order_number"), e)
                              for e in order_node.get("line_items", {}).get("edges", [])]
            else:
                edges = data.get("data", {}).get("totes", {}).get("data", {}).get("edges", [])
                if not edges:
                    final_status = (_("No tote found with ID or Barcode: ") + order_number, True)
                    return
                tote_node = edges[0]["node"]
                orders_list = tote_node.get("orders") or []
                for order_obj in orders_list:
                    items = order_obj.get("line_items", {}).get("edges", [])
                    line_items.extend((order_obj.get("order_number"), e) for e in items)
                if not line_items:
                    final_status = (_("Tote found, but it contains no orders/items."), True)
                    return

            if not hasattr(self, "_lid_defaults"):
                self._lid_defaults = self._load_lid_defaults()

            # The SKU decides what is in the tote. PillcaseOrder decomposes each
            # line item into one row per physical lid (one engraving per lid), so a
            # two-colour bundle arrives as two selectable rows rather than one
            # ambiguous one, and the colour comes from the SKU instead of being
            # guessed out of the marketing name.
            items_to_show = []
            for order_no, item_edge in line_items:
                node = item_edge.get("node") or {}
                for row in PillcaseOrder.resolve_line_item(node):
                    row["order_number"] = order_no or ""
                    items_to_show.append(row)

            if not items_to_show:
                final_status = (_("Found, but no line items available."), True)
                return

            # `complexity` is the credit price ShipHero charged for this query,
            # shown so an expensive query is noticed long before it runs the
            # account out of credits mid-shift.
            result_key = "orders" if searchMode == "Order" else "totes"
            cost = ((data.get("data") or {}).get(result_key) or {}).get("complexity")
            if cost is not None:
                final_status = (_("Loaded {} lid(s) - ShipHero query cost: {} credits")
                                .format(len(items_to_show), cost), False)

            self.app.engraving.load_order(scanned, searchMode, items_to_show)

        except Exception as e:
            final_status = (_("ShipHero connection error: ") + str(e), True)
        finally:
            self.app.config(cursor="")
            if final_status is None:
                self.app.setStatus("")
            else:
                self._scan_status(final_status[0], final_status[1])
                if final_status[1]:
                    # Failed scan: put the operator straight back on the field
                    self._focus_scan_field()

    def show_shiphero_and_asset_config_dialog(self):
        dialog = Toplevel(self)
        dialog.title(_("ShipHero & Asset Configuration"))
        dialog.transient(self)
        # Hidden while it is built, then sized to its content (_fit_and_center).
        dialog.withdraw()

        main_f = Frame(dialog, padx=15, pady=15)
        main_f.pack(expand=YES, fill=BOTH)

        # -- ShipHero API Group --
        api_frame = LabelFrame(main_f, text=_("ShipHero API Integration"), padx=10, pady=10)
        api_frame.pack(fill=X, pady=(0, 10))

        Label(api_frame, text=_("GraphQL Endpoint:")).grid(row=0, column=0, sticky=E, pady=(0, 5), padx=(0, 5))
        endpoint_var = StringVar(value=Utils.getStr("SurfAlign", "shipheroEndpoint", "https://public-api.shiphero.com/graphql"))
        Entry(api_frame, textvariable=endpoint_var, width=50).grid(row=0, column=1, sticky=W, pady=(0, 5))

        Label(api_frame, text=_("Bearer Token:")).grid(row=1, column=0, sticky=E, pady=5, padx=(0, 5))
        token_var = StringVar(value=keyring.get_password("bCNC", "shipheroToken") or "")
        Entry(api_frame, textvariable=token_var, width=50, show="*").grid(row=1, column=1, sticky=W, pady=5)
        
        Label(api_frame, text=_("Search By:")).grid(row=2, column=0, sticky=E, pady=5, padx=(0, 5))
        search_mode_f = Frame(api_frame)
        search_mode_f.grid(row=2, column=1, sticky=W, pady=5)
        
        search_mode_var = StringVar(value=Utils.getStr("SurfAlign", "shipheroSearchMode", "Order"))
        Radiobutton(search_mode_f, text="Order Number", variable=search_mode_var, value="Order").pack(side=LEFT)
        Radiobutton(search_mode_f, text="Tote Barcode/ID", variable=search_mode_var, value="Tote").pack(side=LEFT, padx=(10, 0))

        Label(api_frame, text=_("Credentials are stored securely in Windows Credential Manager."), 
              font=("", 8, "italic"), foreground="gray").grid(row=3, column=1, sticky=W, pady=(2, 0))

        # -- Local Assets Group --
        asset_frame = LabelFrame(main_f, text=_("Local Assets"), padx=10, pady=10)
        asset_frame.pack(fill=X, pady=(0, 15))

        Label(asset_frame, text=_("Lid Color Thumbnails:")).grid(row=0, column=0, sticky=E, pady=5, padx=(0, 5))
        
        thumb_inner_f = Frame(asset_frame)
        thumb_inner_f.grid(row=0, column=1, sticky=W, pady=5)
        
        thumbnails_var = StringVar(value=Utils.getStr("SurfAlign", "thumbnailsDir", ""))
        Entry(thumb_inner_f, textvariable=thumbnails_var, width=40).pack(side=LEFT)
        
        def browse_thumb_dir():
            from tkinter import filedialog
            # Blank field: open on the shipped folder, which is where the files
            # actually are, rather than on whatever directory Tk last used.
            start = thumbnails_var.get().strip() or PillcaseOrder.thumbnail_dirs()[0]
            d = filedialog.askdirectory(parent=dialog, title=_("Select Thumbnails Directory"), initialdir=start)
            if d:
                thumbnails_var.set(d)
                
        Button(thumb_inner_f, text=_("Browse..."), command=browse_thumb_dir).pack(side=LEFT, padx=(5, 0))

        # Leaving this blank is the normal case - the thumbnails ship with the
        # repo - so the dialog says so instead of looking unconfigured.
        Label(asset_frame, text=_("Leave blank to use the thumbnails that ship with bCNC:"),
              font=("", 8, "italic"), foreground="gray").grid(row=1, column=1, sticky=W, pady=(2, 0))
        Label(asset_frame, text=PillcaseOrder.thumbnail_dirs()[0],
              font=("", 8), foreground="gray").grid(row=2, column=1, sticky=W)

        # -- Action Buttons --
        btn_f = Frame(main_f)
        btn_f.pack(fill=X, pady=(10, 0))
        
        success = [False]

        def save():
            self.set_shiphero_credentials(endpoint_var.get().strip(), token_var.get().strip())
            Utils.setStr("SurfAlign", "thumbnailsDir", thumbnails_var.get().strip())
            new_mode = search_mode_var.get()
            Utils.setStr("SurfAlign", "shipheroSearchMode", new_mode)
            engraving = getattr(self.app, "engraving", None)
            if engraving is not None:
                engraving.scan_label.config(text=engraving._scan_label_text())
            success[0] = True
            dialog.destroy()

        def cancel():
            dialog.destroy()

        Button(btn_f, text=_("Cancel"), command=cancel, width=12).pack(side=RIGHT, padx=(5, 0))
        Button(btn_f, text=_("Save"), command=save, width=12).pack(side=RIGHT, padx=(5, 5))

        self._fit_and_center(dialog, min_width=500, min_height=330)
        dialog.deiconify()
        dialog.grab_set()
        
        self.wait_window(dialog)
        return success[0]



    def generateGcode(self):

        print("Generate Gcode")
        if self._preview_id is not None:
            # Generating now covers any refresh still waiting to run
            self.after_cancel(self._preview_id)
            self._preview_id = None
        preview_key = self._preview_key()
        engrave_text = self.engraveText.get()
        work_area_width, work_area_height = 500, 500
        font_path = None
        if self.font_var.get() == "":
            text_font = None
        else:
            text_font = self.font_var.get()
            font_path = self.all_font_dict.get(text_font)
        text_font_size = float(self.fontSize.get())
        lid_positioning_mode = self.textPositioning_var.get()
        if lid_positioning_mode == "Direct":
            text_position_mm = (float(self.posX.get()), float(self.posY.get()), -float(self.engraveDepth.get()))
        else:
            lid_width, lid_height = self.get_lid_dimensions()
            if lid_width is None or lid_height is None:
                # Centring needs a frame. A lid can now exist without one, so say
                # so rather than dividing None by 2 partway through a cut setup.
                messagebox.showerror(
                    _("Lid Size Missing"),
                    _("‘{}’ has no width and length set.\n\nOpen the lid settings "
                      "and fill them in, or switch text positioning to "
                      "Direct.").format(self.lidName.get() or _("No lid selected")))
                return False
            work_area_width, work_area_height = lid_width, lid_height
            text_position_x = (lid_width / 2) + float(self.center_offset_x.get())
            text_position_y = (-1 * (lid_height / 2)) + float(self.center_offset_y.get())
            text_position_mm = (text_position_x, text_position_y, -float(self.engraveDepth.get()))
        layer_height_mm = float(self.layerHeight.get())
        safe_height_mm = float(self.safeHeight.get())
        final_height_mm = float(self.finalHeight.get())
        save_dir = os.path.dirname(__file__)
        rotation_degrees = float(self.rotation.get())
        feedrate_mm = float(self.feedrate.get())
        spindle_rpm = float(self.spindleRPM.get())
        gap_distance_mm = float(self.gapDistance.get())
        y_adjust_factor = float(self.yAdjustFactor.get())
        try:
            gcode_file_path = setup_blender_scene(engrave_text,
                                                  font_path,
                                                   text_font_size,
                                                   text_position_mm,
                                                   rotation_degrees,
                                                   layer_height_mm,
                                                   safe_height_mm,
                                                   save_dir,
                                                   feedrate_mm,
                                                   spindle_rpm,
                                                   final_height_mm,
                                                   work_area_width,
                                                   work_area_height,
                                                   gap_distance_mm,
                                                   y_adjust_factor)
            
            print("Generated Gcode file path:", gcode_file_path)
            
            # Check if the file was actually created
            if not os.path.exists(gcode_file_path):
                messagebox.showerror(_("GCode Generation Error"),
                                   _("GCode file was not created successfully. Please check the parameters and try again."))
                return False

            self.app.load(gcode_file_path)
            print("Loaded Gcode file:", self.app.gcode.filename)
            self._generated_key = preview_key
            engraving = getattr(self.app, "engraving", None)
            if engraving is not None:
                # The text as it stood when the G-code was made is what gets cut,
                # typed or scanned - so that is what a completed run records.
                engraving.on_gcode_generated(engrave_text, self.lidName.get())
            return True

        except Exception as e:
            messagebox.showerror(_("GCode Generation Error"),
                               _("GCode generation failed. Please check the parameters and try again."))
            print(f"GCode generation error: {e}")
            import traceback
            traceback.print_exc()
            return False




    # # -----------------------------------------------------------------------
    def saveConfig(self):
        Utils.setStr("SurfAlign", "engraveText", self.engraveText.get())
        Utils.setStr("SurfAlign", "textFont", self.font_var.get())
        Utils.setFloat("SurfAlign", "fontSize", self.fontSize.get())
        Utils.setFloat("SurfAlign", "posX", self.posX.get())
        Utils.setFloat("SurfAlign", "posY", self.posY.get())
        Utils.setStr("SurfAlign", "textPositioningMode", self.textPositioning_var.get())
        Utils.setStr("SurfAlign", "lidName", self.lidName.get())
        Utils.setStr("SurfAlign", "lidList", ",".join(self.lid_list))
        Utils.setFloat("SurfAlign", "centerOffsetX", self.center_offset_x.get())
        Utils.setFloat("SurfAlign", "centerOffsetY", self.center_offset_y.get())
        Utils.setFloat("SurfAlign", "yAdjustFactor", self.yAdjustFactor.get())
        Utils.setFloat("SurfAlign", "rotation", self.rotation.get())
        Utils.setFloat("SurfAlign", "feedrate", self.feedrate.get())
        Utils.setFloat("SurfAlign", "spindleRPM", self.spindleRPM.get())
        Utils.setFloat("SurfAlign", "engraveDepth", self.engraveDepth.get())
        Utils.setFloat("SurfAlign", "layerHeight", self.layerHeight.get())
        Utils.setFloat("SurfAlign", "safeHeight", self.safeHeight.get())
        Utils.setFloat("SurfAlign", "finalHeight", self.finalHeight.get())
        Utils.setFloat("SurfAlign", "gapDistance", self.gapDistance.get())
        Utils.setStr("SurfAlign", "fontsFolders", ",".join(self.fonts_folders))


    def on_text_positioning_change(self, event):
        """Callback function to show/hide lid selector based on text positioning selection."""
        lid_positioning_mode = self.textPositioning_var.get()
        lid_center_widgets = [self.lid_label, self.lidName_selector, self.edit_lid_button, self.center_offset_label, self.center_offset_x, self.center_offset_y]
        pos_widgets = [self.pos_label, self.posX, self.posY]
        
        if lid_positioning_mode == "Lid Center":
            [widget.grid() for widget in lid_center_widgets]
            [widget.grid_remove() for widget in pos_widgets]
        elif lid_positioning_mode == "Direct":
            [widget.grid_remove() for widget in lid_center_widgets]
            [widget.grid() for widget in pos_widgets]
        self.nudge_center_btn.config(state=DISABLED if lid_positioning_mode == "Direct" else NORMAL)
        self._update_lid_display()

    def show_edit_lid_dialog(self):
        """Compact popup to manage lids and per-lid defaults (Font Size, Depth, Layer Height)."""
        # Ensure defaults map exists in memory
        if not hasattr(self, "_lid_defaults"):
            self._lid_defaults = self._load_lid_defaults()

        # local helper to parse floats
        def _float_or_none(v):
            try:
                return float(v)
            except Exception:
                return None

        dialog = Toplevel(self)
        dialog.title(_("Lids & Settings"))
        dialog.resizable(True, True)
        dialog.transient(self)
        # Hidden while it is built, then sized to its content (_fit_and_center).
        dialog.withdraw()

        # ====== Top row: Add new lid (inline) ======
        top = Frame(dialog)
        top.pack(fill=X, padx=10, pady=(10, 6))

        Label(top, text=_("Lid:")).grid(row=0, column=0, sticky=E, padx=(0, 6))
        new_lid_entry = Entry(top, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=32)
        new_lid_entry.grid(row=0, column=1, sticky=EW)
        new_lid_entry.focus_set()

        # subtle inline hint (space-saving vs long paragraphs)
        hint = Label(top,
                     text=_("Any name  e.g.  Weekly Vitamin XL   "
                            "(size is set on the right, in mm)"),
                     fg="gray", font=("TkDefaultFont", 8))
        hint.grid(row=1, column=1, columnspan=2, sticky=W, pady=(0, 4))

        # The product this lid engraves, picked from the SKU grammar rather than
        # typed. A scanned SKU is decomposed to a product code, so this is the key
        # the order popup matches on - free text could not be matched reliably.
        Label(top, text=_("Product:")).grid(row=2, column=0, sticky=E, padx=(0, 6), pady=(4, 0))
        df_product = ttk.Combobox(top, values=[""] + PillcaseOrder.product_choices(),
                                  state="readonly", width=30)
        df_product.grid(row=2, column=1, sticky=EW, pady=(4, 0))

        add_btn = Button(top, text=_("➕ Add Lid"), width=14, padx=8, pady=2)
        add_btn.grid(row=0, column=2, rowspan=3, padx=(8, 0), sticky=W)

        top.columnconfigure(1, weight=1)

        # ====== Main content: left (list) | right (defaults) ======
        content = Frame(dialog)
        content.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))

        # -- LEFT: Existing lids + Delete
        left = LabelFrame(content, text=_("Existing Lids"), padx=8, pady=8)
        left.pack(side=LEFT, fill=BOTH, expand=True)

        list_frame = Frame(left)
        list_frame.pack(fill=BOTH, expand=True)
        # Extended selection (Ctrl/Shift-click) so a set of lids can be deleted
        # in one go rather than one confirm-and-acknowledge round at a time.
        lid_listbox = tkinter.Listbox(list_frame, height=10, selectmode="extended")
        lid_listbox.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar = tkinter.Scrollbar(list_frame, orient=VERTICAL, command=lid_listbox.yview)
        scrollbar.pack(side=RIGHT, fill=Y)
        lid_listbox.config(yscrollcommand=scrollbar.set)

        # Fill list
        for lid in self.lid_list:
            lid_listbox.insert(END, lid)

        delete_row = Frame(left)
        delete_row.pack(pady=(8, 0), anchor="w")
        delete_btn = Button(delete_row, text=_("🗑 Delete Selected"),
                            bg="#F44336", fg="white", padx=8, pady=2)
        delete_btn.pack(side=LEFT)
        tkExtra.Balloon.set(delete_btn, _("Ctrl-click or Shift-click to select several lids"))
        delete_all_btn = Button(delete_row, text=_("Delete All"),
                                bg="#B71C1C", fg="white", padx=8, pady=2)
        delete_all_btn.pack(side=LEFT, padx=(8, 0))

        # -- RIGHT: Defaults (compact grid)
        right = LabelFrame(content, text=_("Settings (selected lid)"), padx=8, pady=8)
        right.pack(side=LEFT, fill=Y, padx=(10, 0))

        # The engraving frame. It used to be encoded in the lid's name; here it is
        # editable, which is the point — the text is centred at (W/2, -L/2), so a
        # lid whose text should sit off-centre is tuned by shrinking the frame
        # rather than by renaming the lid.
        Label(right, text=_("Width X (mm):")).grid(row=0, column=0, sticky=E, padx=(0, 6), pady=(0, 4))
        df_width = tkExtra.FloatEntry(right, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=10)
        df_width.grid(row=0, column=1, sticky=W, pady=(0, 4))

        Label(right, text=_("Length Y (mm):")).grid(row=1, column=0, sticky=E, padx=(0, 6), pady=(0, 4))
        df_length = tkExtra.FloatEntry(right, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=10)
        df_length.grid(row=1, column=1, sticky=W, pady=(0, 4))

        Label(right, text=_("Font Size (mm):")).grid(row=2, column=0, sticky=E, padx=(0, 6), pady=(0, 4))
        df_font_size = tkExtra.FloatEntry(right, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=10)
        df_font_size.grid(row=2, column=1, sticky=W, pady=(0, 4))

        Label(right, text=_("Depth (mm):")).grid(row=3, column=0, sticky=E, padx=(0, 6), pady=(0, 4))
        df_depth = tkExtra.FloatEntry(right, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=10)
        df_depth.grid(row=3, column=1, sticky=W, pady=(0, 4))

        Label(right, text=_("Layer Height (mm):")).grid(row=4, column=0, sticky=E, padx=(0, 6), pady=(0, 4))
        df_layer = tkExtra.FloatEntry(right, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=10)
        df_layer.grid(row=4, column=1, sticky=W, pady=(0, 4))

        # NEW: Rotation default
        Label(right, text=_("Rotation (deg):")).grid(row=5, column=0, sticky=E, padx=(0, 6), pady=(0, 4))
        df_rotation = tkExtra.FloatEntry(right, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=10)
        df_rotation.grid(row=5, column=1, sticky=W, pady=(0, 4))

        # Compact buttons row (shifted down by 1)
        btns = Frame(right)
        btns.grid(row=6, column=0, columnspan=2, sticky=W, pady=(8, 0))
        save_btn = Button(btns, text=_("💾 Save"), padx=8, pady=2)
        save_btn.pack(side=LEFT)
        clear_btn = Button(btns, text=_("Clear"), padx=8, pady=2)
        clear_btn.pack(side=LEFT, padx=(8, 0))

        # ====== Bottom: Close ======
        bottom = Frame(dialog)
        bottom.pack(fill=X, padx=10, pady=(0, 10))

        def export_data():
            import json
            import tkinter.filedialog
            f = tkinter.filedialog.asksaveasfilename(parent=dialog, title=_("Export Lids & Settings"), defaultextension=".json", filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
            if f:
                try:
                    with open(f, 'w', encoding='utf-8') as out:
                        json.dump({"lids": self.lid_list, "defaults": self._lid_defaults}, out, indent=2)
                    messagebox.showinfo(_("Export Successful"), _("Exported lids and settings to\n{}").format(f), parent=dialog)
                except Exception as e:
                    messagebox.showerror(_("Export Error"), str(e), parent=dialog)

        def import_data():
            import json
            import tkinter.filedialog
            f = tkinter.filedialog.askopenfilename(parent=dialog, title=_("Import Lids & Settings"), filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
            if f:
                try:
                    with open(f, 'r', encoding='utf-8') as inp:
                        data = json.load(inp)
                    
                    changed = False
                    
                    # Check for conflicts
                    import_defaults = data.get("defaults", {})
                    # Two lids claiming the same product code would both answer for
                    # a scanned SKU, so that is a conflict just as a duplicate name is.
                    # Codes are compared as PillcaseOrder resolves them, so an imported
                    # lid still on the old free-text caseType is caught too.
                    existing_codes = set()
                    for cfg in self._lid_defaults.values():
                        existing_codes |= PillcaseOrder.lid_product_codes(cfg)

                    conflicts = []
                    for lid, cfg in import_defaults.items():
                        codes = PillcaseOrder.lid_product_codes(cfg)
                        if lid in self._lid_defaults or (codes & existing_codes):
                            conflicts.append(lid)
                    overwrite = True
                    if conflicts:
                        conflict_str = "\n".join("• " + lid for lid in conflicts[:10])
                        if len(conflicts) > 10:
                            conflict_str += "\n" + _("...and {} more").format(len(conflicts) - 10)
                        
                        msg = _("{} lids already exist or conflict:\n\n{}\n\nDo you want to overwrite their settings?").format(len(conflicts), conflict_str)
                        overwrite = messagebox.askyesno(
                            _("Conflicts Detected"),
                            msg,
                            parent=dialog
                        )

                    if not overwrite:
                        # Filter out conflicting defaults and lids
                        if "defaults" in data:
                            data["defaults"] = {k: v for k, v in data["defaults"].items() if k not in conflicts}
                        if "lids" in data:
                            data["lids"] = [k for k in data["lids"] if k not in conflicts]
                    else:
                        # Overwrite is True. Remove old lids that claim the same product
                        for lid in conflicts:
                            cfg = import_defaults.get(lid, {})
                            codes = PillcaseOrder.lid_product_codes(cfg)
                            if codes:
                                # Find ALL existing lids claiming this product and drop them
                                old_lids_to_remove = [old_lid for old_lid, old_cfg in self._lid_defaults.items()
                                                      if (PillcaseOrder.lid_product_codes(old_cfg) & codes)
                                                      and old_lid != lid]
                                for old_lid in old_lids_to_remove:
                                    del self._lid_defaults[old_lid]
                                    if old_lid in self.lid_list:
                                        self.lid_list.remove(old_lid)
                                        # Update listbox
                                        items = lid_listbox.get(0, END)
                                        if old_lid in items:
                                            lid_listbox.delete(items.index(old_lid))
                                    changed = True


                    if "lids" in data:
                        for lid in data["lids"]:
                            if lid not in self.lid_list:
                                self.lid_list.append(lid)
                                lid_listbox.insert(END, lid)
                                changed = True
                                
                    if "defaults" in data:
                        for lid, cfg in data["defaults"].items():
                            self._lid_defaults[lid] = cfg
                            changed = True
                            if lid not in self.lid_list:
                                self.lid_list.append(lid)
                                lid_listbox.insert(END, lid)
                                changed = True
                    
                    if changed:
                        self._save_lid_defaults()
                        self.saveConfig()
                        if hasattr(self, 'lidName_selector'):
                            self.lidName_selector['values'] = self.lid_list
                            
                    messagebox.showinfo(_("Import Successful"), _("Imported lids and settings from\n{}").format(f), parent=dialog)
                except Exception as e:
                    messagebox.showerror(_("Import Error"), str(e), parent=dialog)

        Button(bottom, text=_("📤 Export"), command=export_data, padx=8, pady=2).pack(side=LEFT, padx=(0, 5))
        Button(bottom, text=_("📥 Import"), command=import_data, padx=8, pady=2).pack(side=LEFT)
        Button(bottom, text=_("Close"), command=dialog.destroy, padx=10, pady=2).pack(side=RIGHT)

        # ====== Helpers (selection & defaults I/O) ======
        def get_selected_lid():
            # The settings panel edits ONE lid. With several selected (for a bulk
            # delete) there is no single lid to show or save, so report none
            # rather than silently acting on whichever happens to be first.
            sel = lid_listbox.curselection()
            return lid_listbox.get(sel[0]) if len(sel) == 1 else None

        def load_defaults_ui_for(lid_name):
            # blank first (space-saving + clarity)
            df_width.set("")
            df_length.set("")
            df_font_size.set("")
            df_depth.set("")
            df_layer.set("")
            df_rotation.set("")  # NEW
            df_product.set("")
            if not lid_name:
                return
            cfg = self._lid_defaults.get(lid_name, {})
            if isinstance(cfg, dict):
                dims = PillcaseOrder.lid_dimensions(cfg)
                if dims:
                    df_width.set(dims[0])
                    df_length.set(dims[1])
                if cfg.get("fontSize") is not None:
                    df_font_size.set(cfg["fontSize"])
                if cfg.get("depth") is not None:
                    df_depth.set(cfg["depth"])
                if cfg.get("layerHeight") is not None:
                    df_layer.set(cfg["layerHeight"])
                if cfg.get("rotation") is not None:   # NEW
                    df_rotation.set(cfg["rotation"])
                # An older lid carries only a free-text caseType; resolving it
                # through the parser preselects the right product, so an existing
                # setup does not have to be re-entered by hand.
                df_product.set(PillcaseOrder.choice_for_code(
                    PillcaseOrder.configured_product_code(cfg)))

        def save_defaults_for_selected():
            lid_name = get_selected_lid()
            if not lid_name:
                messagebox.showwarning(_("No Selection"), _("Select a single lid to save settings."), parent=dialog)
                return
            width, length = _float_or_none(df_width.get()), _float_or_none(df_length.get())
            if (width is not None and width <= 0) or (length is not None and length <= 0):
                messagebox.showwarning(_("Invalid Size"),
                                       _("Width and length must be positive numbers."),
                                       parent=dialog)
                return
            cfg = dict(self._lid_defaults.get(lid_name, {}))
            cfg["width"]       = width
            cfg["length"]      = length
            cfg["fontSize"]    = _float_or_none(df_font_size.get())
            cfg["depth"]       = _float_or_none(df_depth.get())
            cfg["layerHeight"] = _float_or_none(df_layer.get())
            cfg["rotation"]    = _float_or_none(df_rotation.get())
            cfg["productCode"] = PillcaseOrder.code_from_choice(df_product.get()) or None
            self._lid_defaults[lid_name] = cfg
            self._save_lid_defaults()
            # The outline on the canvas is drawn from these numbers, so redraw it
            # when the lid being edited is the one currently selected.
            if self.lidName.get() == lid_name:
                self._on_main_lid_changed()
            messagebox.showinfo(_("Saved"), _("Settings saved for ‘{}’.").format(lid_name), parent=dialog)

        def clear_defaults_for_selected():
            lid_name = get_selected_lid()
            if not lid_name:
                messagebox.showwarning(_("No Selection"), _("Select a single lid to clear settings."), parent=dialog)
                return
            if lid_name in self._lid_defaults:
                for k in ("width", "length", "fontSize", "depth", "layerHeight",
                          "rotation", "productCode", "skuKeyword", "caseType"):
                    self._lid_defaults[lid_name].pop(k, None)
                if not self._lid_defaults[lid_name]:
                    del self._lid_defaults[lid_name]
                self._save_lid_defaults()
            # blank fields
            df_width.set("")
            df_length.set("")
            df_font_size.set("")
            df_depth.set("")
            df_layer.set("")
            df_rotation.set("")
            df_product.set("")
            messagebox.showinfo(_("Cleared"), _("Settings cleared for ‘{}’.").format(lid_name), parent=dialog)

        # ====== Add/Delete (with blanking) ======
        def add_and_blank(*_):
            name = new_lid_entry.get().strip()
            product = PillcaseOrder.code_from_choice(df_product.get())
            if not name:
                return
            self.add_lid_from_dialog(name, dialog, lid_listbox)
            # select the newly added lid
            try:
                idx = self.lid_list.index(name)
                lid_listbox.selection_clear(0, END)
                lid_listbox.selection_set(idx)
                lid_listbox.see(idx)
            except Exception:
                pass
            
            # Carry whatever is already filled in on the right onto the new lid,
            # so a lid can be created complete in one pass rather than added and
            # then selected and saved.
            width, length = _float_or_none(df_width.get()), _float_or_none(df_length.get())
            cfg = dict(self._lid_defaults.get(name, {}))
            for key, value in (("productCode", product or None),
                               ("width", width), ("length", length),
                               ("fontSize", _float_or_none(df_font_size.get())),
                               ("depth", _float_or_none(df_depth.get())),
                               ("layerHeight", _float_or_none(df_layer.get())),
                               ("rotation", _float_or_none(df_rotation.get()))):
                if value is not None:
                    cfg[key] = value
            if cfg:
                self._lid_defaults[name] = cfg
                self._save_lid_defaults()

            # blank defaults inputs, but keep the product so a run of lids for the
            # same case can be added without re-picking it each time
            df_width.set("")
            df_length.set("")
            df_font_size.set("")
            df_depth.set("")
            df_layer.set("")
            df_rotation.set("")  # NEW
            df_product.set(PillcaseOrder.choice_for_code(product))

        def blank_settings():
            for field in (df_width, df_length, df_font_size, df_depth,
                          df_layer, df_rotation, df_product):
                field.set("")

        # Blank only when something was actually deleted: cancelling the
        # confirmation should leave the panel showing what it showed.
        def delete_and_blank():
            if self.delete_lid_from_dialog(lid_listbox, dialog):
                lid_listbox.selection_clear(0, END)
                blank_settings()

        def delete_all_and_blank():
            if self.delete_all_lids_from_dialog(lid_listbox, dialog):
                blank_settings()

            # Wire buttons
        add_btn.config(command=add_and_blank)
        delete_btn.config(command=delete_and_blank)
        delete_all_btn.config(command=delete_all_and_blank)
        save_btn.config(command=save_defaults_for_selected)
        clear_btn.config(command=clear_defaults_for_selected)

        # Selection behavior
        lid_listbox.bind('<<ListboxSelect>>', lambda _e=None: load_defaults_ui_for(get_selected_lid()))
        new_lid_entry.bind('<Return>', add_and_blank)
        dialog.bind('<Escape>', lambda _e=None: dialog.destroy())

        # Preselect current lid if present
        try:
            if self.lidName.get():
                idx = self.lid_list.index(self.lidName.get())
                lid_listbox.selection_clear(0, END)
                lid_listbox.selection_set(idx)
                lid_listbox.see(idx)
                load_defaults_ui_for(self.lidName.get())
        except Exception:
            pass

        # Measure BEFORE the pack_propagate calls below: they stop the list from
        # reporting its size, and the window would be fitted without it. Never
        # smaller than the original 560x360 layout ("wider, shorter").
        self._fit_and_center(dialog, min_width=560, min_height=360)

        # Resize behavior
        left.pack_propagate(False)
        list_frame.pack_propagate(False)

        dialog.deiconify()
        dialog.grab_set()

    def validate_lid_name(self, lid_name):
        """Reject a lid name the config cannot round-trip. None when it is fine.

        A name is now just a label — the engraving frame is stored beside it — so
        the only real constraints are that it exists and survives storage. The
        lid list is persisted as one comma-joined ini value, so a comma in a name
        would silently split it into two lids that own no settings.
        """
        name = (lid_name or "").strip()
        if not name:
            return _("Please enter a lid name.")
        if "," in name:
            return _("Lid name cannot contain a comma.")
        return None  # Valid

    def add_lid_from_dialog(self, new_lid_name, dialog, lid_listbox):
        """Add a new lid name from the dialog and close it."""
        if not new_lid_name:
            messagebox.showwarning(_("Empty Entry"), _("Please enter a lid name."), parent=dialog)
            return
        
        error_message = self.validate_lid_name(new_lid_name)
        if error_message:
            messagebox.showwarning(_("Invalid Format"), error_message, parent=dialog)
            return
        
        if new_lid_name in self.lid_list:
            messagebox.showwarning(_("Duplicate Entry"), _("This lid name already exists in the list."), parent=dialog)
            return

        self.lid_list.append(new_lid_name)
        self.lidName_selector['values'] = self.lid_list
        self.lidName.set(new_lid_name)
        
        # Refresh the listbox
        lid_listbox.delete(0, END)
        for lid in self.lid_list:
            lid_listbox.insert(END, lid)
        
        self.saveConfig()
        messagebox.showinfo(_("Success"), _("Lid name '{}' has been added to the list.").format(new_lid_name), parent=dialog)

    def delete_lid_from_dialog(self, lid_listbox, dialog):
        """Delete every lid selected in the dialog's list. True when deleted."""
        selected = [lid_listbox.get(i) for i in lid_listbox.curselection()]
        if not selected:
            messagebox.showwarning(_("No Selection"),
                                   _("Please select one or more lids to delete."),
                                   parent=dialog)
            return False
        return self._confirm_and_delete_lids(selected, lid_listbox, dialog)

    def delete_all_lids_from_dialog(self, lid_listbox, dialog):
        """Delete every configured lid. True when deleted."""
        if not self.lid_list:
            messagebox.showinfo(_("No Lids"), _("There are no lids to delete."), parent=dialog)
            return False
        return self._confirm_and_delete_lids(list(self.lid_list), lid_listbox, dialog,
                                             everything=True)

    def _confirm_and_delete_lids(self, lids, lid_listbox, dialog, everything=False):
        """Remove `lids` and their saved settings as one batch. True when deleted.

        One confirmation and one save for the whole batch, and no success popup —
        the list visibly updating is the confirmation. Deleting used to cost a
        confirm AND an acknowledgement per lid, which is what made clearing out
        an old set of lids slow.
        """
        if everything:
            prompt = _("Delete all {} lids and their settings?\n\n"
                       "This cannot be undone. Use Export first if you may want "
                       "them back.").format(len(lids))
        else:
            shown = "\n".join("• " + lid for lid in lids[:10])
            if len(lids) > 10:
                shown += "\n" + _("...and {} more").format(len(lids) - 10)
            prompt = _("Delete {} lid(s) and their settings?\n\n{}").format(len(lids), shown)
        if not messagebox.askyesno(_("Confirm Delete"), prompt, parent=dialog):
            return False

        doomed = set(lids)
        self.lid_list[:] = [lid for lid in self.lid_list if lid not in doomed]
        if not hasattr(self, "_lid_defaults"):
            self._lid_defaults = self._load_lid_defaults()
        for lid in doomed:
            self._lid_defaults.pop(lid, None)
        self._save_lid_defaults()

        self.lidName_selector['values'] = self.lid_list
        lid_listbox.delete(0, END)
        for lid in self.lid_list:
            lid_listbox.insert(END, lid)

        if self.lidName.get() in doomed:
            self.lidName.set("")
            # No lid is selected any more, so its outline must not linger.
            self._on_main_lid_changed()

        self.saveConfig()
        return True

    def show_add_font_folder_dialog(self):
        """Show a popup dialog for adding font folder paths."""
        dialog = Toplevel(self)
        dialog.title(_("Add Font Folder"))
        dialog.geometry("500x600")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("+%d+%d" % (self.winfo_rootx() + 50, self.winfo_rooty() + 50))
        
        # Add new font folder section
        add_frame = LabelFrame(dialog, text=_("Add New Font Folder"), padx=10, pady=10)
        add_frame.pack(fill=X, padx=10, pady=(10, 5))
        
        Label(add_frame, text=_("Font Folder Path:")).grid(row=0, column=0, sticky=W, pady=(0, 5))
        new_folder_entry = Entry(add_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=50)
        new_folder_entry.grid(row=1, column=0, columnspan=2, sticky=EW, pady=(0, 5))
        new_folder_entry.focus_set()
        
        # Browse button
        browse_button = Button(add_frame, text=_("Browse"), command=lambda: self.browse_font_folder(new_folder_entry), padx=10, pady=2)
        browse_button.grid(row=1, column=2, sticky=W, padx=(5, 0))
        
        Label(add_frame, text=_("Note: Folder should contain .ttf or .otf font files"), 
              fg="blue", font=("TkDefaultFont", 8)).grid(row=2, column=0, columnspan=3, sticky=W, pady=(0, 5))
        
        add_button = Button(add_frame, text=_("Add"), command=lambda: self.add_font_folder_from_dialog(new_folder_entry.get().strip(), dialog, folder_listbox), padx=10, pady=2)
        add_button.grid(row=3, column=0, sticky=W, pady=(5, 0))
        
        # Existing font folders section
        existing_frame = LabelFrame(dialog, text=_("Existing Font Folders"), padx=10, pady=10)
        existing_frame.pack(fill=BOTH, expand=True, padx=10, pady=(5, 10))
        
        # Listbox with scrollbar for existing folders
        list_frame = Frame(existing_frame)
        list_frame.pack(fill=BOTH, expand=True)
        
        folder_listbox = tkinter.Listbox(list_frame, height=8)
        folder_listbox.pack(side=LEFT, fill=BOTH, expand=True)
        
        scrollbar = tkinter.Scrollbar(list_frame, orient=VERTICAL, command=folder_listbox.yview)
        scrollbar.pack(side=RIGHT, fill=Y)
        folder_listbox.config(yscrollcommand=scrollbar.set)
        
        # Populate listbox with existing folders
        for folder in self.fonts_folders:
            folder_listbox.insert(END, folder)
        
        # Delete button
        delete_button = Button(existing_frame, text=_("Delete Selected"), 
                              command=lambda: self.delete_font_folder_from_dialog(folder_listbox, dialog), 
                              padx=10, pady=2, bg="#F44336", fg="white")
        delete_button.pack(pady=(5, 0))
        
        # Bottom buttons
        button_frame = Frame(dialog)
        button_frame.pack(fill=X, padx=10, pady=(0, 10))
        
        Button(button_frame, text=_("Close"), command=dialog.destroy, padx=10, pady=2).pack(side=RIGHT)
        
        # Bind events
        new_folder_entry.bind('<Return>', lambda event: self.add_font_folder_from_dialog(new_folder_entry.get().strip(), dialog, folder_listbox))
        dialog.bind('<Escape>', lambda event: dialog.destroy())
        
        add_frame.columnconfigure(1, weight=1)

    def browse_font_folder(self, entry_widget):
        """Open a folder browser dialog to select a font folder."""
        from tkinter import filedialog
        folder_path = filedialog.askdirectory(title=_("Select Font Folder"))
        if folder_path:
            entry_widget.delete(0, END)
            entry_widget.insert(0, folder_path)

    def add_font_folder_from_dialog(self, new_folder_path, dialog, folder_listbox):
        """Add a new font folder from the dialog."""
        if not new_folder_path:
            messagebox.showwarning(_("Empty Entry"), _("Please enter a folder path."), parent=dialog)
            return
        
        if not os.path.exists(new_folder_path):
            messagebox.showwarning(_("Invalid Path"), _("The specified folder does not exist."), parent=dialog)
            return
        
        if not os.path.isdir(new_folder_path):
            messagebox.showwarning(_("Invalid Path"), _("The specified path is not a folder."), parent=dialog)
            return
        
        # Check if folder contains font files
        font_files = [f for f in os.listdir(new_folder_path) if f.lower().endswith(('.ttf', '.otf'))]
        if not font_files:
            messagebox.showwarning(_("No Font Files"), _("The selected folder does not contain any .ttf or .otf font files."), parent=dialog)
            return
        
        if new_folder_path in self.fonts_folders:
            messagebox.showwarning(_("Duplicate Entry"), _("This font folder already exists in the list."), parent=dialog)
            return

        self.fonts_folders.append(new_folder_path)
        
        # Refresh the listbox
        folder_listbox.delete(0, END)
        for folder in self.fonts_folders:
            folder_listbox.insert(END, folder)
        
        # Refresh fonts immediately after adding the folder
        try:
            # Reload fonts from all folders
            self.all_font_dict = {}
            for fonts_folder in self.fonts_folders:
                self.all_font_dict.update(self.load_fonts_from_folder(fonts_folder))
            
            # Reload system fonts from registry
            self.all_font_dict.update(self.load_fonts_from_registry())
            
            # Update the font selector
            font_list = sorted(set(self.all_font_dict.keys()))
            self.font_selector['values'] = font_list
            
            self.saveConfig()
            messagebox.showinfo(_("Success"), _("Font folder '{}' has been added and fonts refreshed successfully.").format(new_folder_path), parent=dialog)
        except Exception as e:
            messagebox.showerror(_("Error"), _("Font folder added but failed to refresh fonts: {}").format(str(e)), parent=dialog)

    def delete_font_folder_from_dialog(self, folder_listbox, dialog):
        """Delete a selected font folder from the list."""
        selected_index = folder_listbox.curselection()
        if not selected_index:
            messagebox.showwarning(_("No Selection"), _("Please select a font folder to delete."), parent=dialog)
            return
            
        selected_folder = folder_listbox.get(selected_index[0])
        
        # Confirm deletion
        if not messagebox.askyesno(_("Confirm Delete"), 
                                  _("Are you sure you want to delete '{}'?").format(selected_folder), 
                                  parent=dialog):
            return
            
        self.fonts_folders.remove(selected_folder)
        
        # Refresh the listbox
        folder_listbox.delete(0, END)
        for folder in self.fonts_folders:
            folder_listbox.insert(END, folder)
        
        # Refresh fonts immediately after deleting the folder
        try:
            # Reload fonts from all remaining folders
            self.all_font_dict = {}
            for fonts_folder in self.fonts_folders:
                self.all_font_dict.update(self.load_fonts_from_folder(fonts_folder))
            
            # Reload system fonts from registry
            self.all_font_dict.update(self.load_fonts_from_registry())
            
            # Update the font selector
            font_list = sorted(set(self.all_font_dict.keys()))
            self.font_selector['values'] = font_list
            
            self.saveConfig()
            messagebox.showinfo(_("Success"), _("Font folder '{}' has been deleted and fonts refreshed successfully.").format(selected_folder), parent=dialog)
        except Exception as e:
            messagebox.showerror(_("Error"), _("Font folder deleted but failed to refresh fonts: {}").format(str(e)), parent=dialog)



    def get_lid_dimensions(self):
        """(width, height) in mm of the selected lid's engraving frame.

        Returns (None, None) when no lid is selected or it has no frame set yet.
        """
        lid_name = self.lidName.get()
        if not lid_name:
            return None, None
        if not hasattr(self, "_lid_defaults"):
            self._lid_defaults = self._load_lid_defaults()
        dims = PillcaseOrder.lid_dimensions(self._lid_defaults.get(lid_name, {}))
        return dims if dims else (None, None)

    def show_text_syntax_help(self):
        """Show a popup window with explanation of text syntax."""
        help_window = tk.Toplevel(self)
        help_window.title(_("Text Syntax Help"))
        help_window.geometry("500x300")
        help_window.resizable(False, False)
        
        # Make the window modal
        help_window.transient(self)
        help_window.grab_set()
        
        # Center the window
        help_window.update_idletasks()
        x = (help_window.winfo_screenwidth() // 2) - (500 // 2)
        y = (help_window.winfo_screenheight() // 2) - (300 // 2)
        help_window.geometry(f"500x300+{x}+{y}")
        
        # Main frame
        main_frame = tk.Frame(help_window)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)
        
        # Title
        title_label = tk.Label(main_frame, text=_("Text Syntax Guide"), 
                              font=("TkDefaultFont", 12, "bold"))
        title_label.pack(pady=(0, 15))
        
        # Content frame
        content_frame = tk.Frame(main_frame)
        content_frame.pack(fill=tk.BOTH, expand=True)
        
        # Pipe separator section
        tk.Label(content_frame, text=_("| = Word Spacing"), font=("TkDefaultFont", 10, "bold")).pack(anchor=tk.W)
        tk.Label(content_frame, text="Hello|World → Two objects with spacing", font=("Courier", 9)).pack(anchor=tk.W, padx=(10, 0))
        
        # Literal pipe section
        tk.Label(content_frame, text=_("<|> = Literal Pipe"), font=("TkDefaultFont", 10, "bold")).pack(anchor=tk.W, pady=(10, 0))
        tk.Label(content_frame, text="Hello<|>World → Single object: 'Hello|World'", font=("Courier", 9)).pack(anchor=tk.W, padx=(10, 0))
        
        # Gap distance info
        tk.Label(content_frame, text=_("Gap Distance: Use '| Sep Gap (mm)' field"), font=("TkDefaultFont", 10, "bold")).pack(anchor=tk.W, pady=(10, 0))
        
        # Close button
        close_button = tk.Button(main_frame, text=_("Close"), command=help_window.destroy, width=8)
        close_button.pack(pady=(15, 0))
        
        # Focus on the window
        help_window.focus_set()


class MultiPointProbe(CNCRibbon.PageFrame):
    def __init__(self, master, app, run_bar=None):
        CNCRibbon.PageFrame.__init__(self, master, "MultiPointProbe", app)

        self.probe_points = []
        self.stop_quick_align = False
        # True from Align & Run until the program is sent (or the sequence ends):
        # G-code must not be regenerated while it is being probed and aligned.
        self.quick_align_active = False

        # Track scheduled callbacks so we can cancel them on stop
        self._poll_id = None
        self._process_id = None
        self._run_id = None
        self._deploy_delay_id = None
        self._retract_id = None  # for delayed retract after hard stop
        self.probe_deployed = False  # Track probe state

        # === UI (unchanged layout, trimmed for brevity – keep yours) ===
        lframe = tkExtra.ExLabelFrame(self, text=_("Multi-Point Surface Probe"), foreground="DarkBlue")
        lframe.pack(side=TOP, fill=X)
        frame = lframe.frame

        row, col = 0, 0
        # 👇 keep a reference to the label so we can hide/show it later
        self.n_probe_points_label = Label(frame, text=_("No. of Points:"))
        self.n_probe_points_label.grid(row=row, column=col, sticky=E)

        col += 1
        self.n_probe_points = tkExtra.IntegerEntry(frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND)
        self.n_probe_points.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(self.n_probe_points, _("Number of probe points"))

        row += 1
        col = 0
        Label(frame, text=_("Z Min, Max:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.mp_z_min = tkExtra.FloatEntry(frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND)
        self.mp_z_min.grid(row=row, column=col, sticky=EW)
        col += 1
        self.mp_z_max = tkExtra.FloatEntry(frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND)
        self.mp_z_max.grid(row=row, column=col, sticky=EW)

        row += 1
        col = 0
        Label(frame, text=_("Probe Coverage Method")).grid(row=row, column=col, sticky=E)
        col += 1

        self.probe_coverage_methods = ["EvenCoverage", "AreaCoverage", "BilinearGrid"]

        self.probe_coverage_method_var = tk.StringVar()
        self.probe_coverage_method = ttk.Combobox(
            frame,
            textvariable=self.probe_coverage_method_var,
            values=self.probe_coverage_methods,
            width=16,
            state="readonly"
        )
        self.probe_coverage_method.grid(row=row, column=col, sticky=EW)
        self.probe_coverage_method_var.set("EvenCoverage")

        # Binding that **always** works
        self.probe_coverage_method.bind("<<ComboboxSelected>>", self._on_probe_method_change)

        # Register to disable during run
        self.addWidget(self.probe_coverage_method)

        row += 1
        col = 0
        self.bilin_grid_cell_label = Label(frame, text=_("Cell ~Size (mm):"))
        self.bilin_grid_cell_label.grid(row=row, column=col, sticky=E)

        col += 1
        self.bilin_grid_cell_size_mm = tkExtra.FloatEntry(
            frame, 
            background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.bilin_grid_cell_size_mm.set(5.0)  # Set a default value, e.g., 5.0 mm
        self.bilin_grid_cell_size_mm.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(self.bilin_grid_cell_size_mm, _("Approx desired bilinear grid cell size"))
        self.addWidget(self.bilin_grid_cell_size_mm)

        row += 1
        col = 0
        
        # Create a LabelFrame for the Offset section
        offset_frame = LabelFrame(frame, text=_("Offset (Probe → Tool)"), padx=5, pady=5)
        offset_frame.grid(row=row, column=0, columnspan=4, sticky=EW, padx=5, pady=5)
        
        # Offset values row (first row)
        offset_row = 0
        self.x_probe_to_tool_offset = tkExtra.FloatEntry(offset_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND)
        self.x_probe_to_tool_offset.grid(row=offset_row, column=0, sticky=EW, padx=2)
        self.y_probe_to_tool_offset = tkExtra.FloatEntry(offset_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND)
        self.y_probe_to_tool_offset.grid(row=offset_row, column=1, sticky=EW, padx=2)
        self.z_probe_to_tool_offset = tkExtra.FloatEntry(offset_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND)
        self.z_probe_to_tool_offset.grid(row=offset_row, column=2, sticky=EW, padx=2)
        
        
        # Buttons row inside the LabelFrame (second row, below offsets)
        btn_row = 1
        
        # Measure Z (Bitsetter) Button
        b = Button(offset_frame, text=_("Measure Z (Bitsetter)"), command=self.measure_z_offset_bitsetter_popup, padx=1, pady=1)
        b.grid(row=btn_row, column=0, sticky=EW, padx=2)
        tkExtra.Balloon.set(b, _("Measure Z Offset using a Bitsetter"))

        # Set Tool Height Button (Physical Calibration)
        b = Button(offset_frame, text=_("Set Tool Height"), command=self.set_tool_height_popup, padx=1, pady=1)
        b.grid(row=btn_row, column=1, sticky=EW, padx=2)
        tkExtra.Balloon.set(b, _("Physical tool height calibration using probe\nAttach cutting bit loosely first."))
        
        # Measure Z Offset Button
        b = Button(offset_frame, text=_("Measure Z Offset"), command=self.measure_z_offset_popup, padx=1, pady=1)
        b.grid(row=btn_row, column=2, sticky=EW, padx=2)
        tkExtra.Balloon.set(b, _("Measure Z Offset between Tool and Probe\nTool must be touching the surface."))
        
        # Configure column weights for offset_frame to make widgets expand properly
        offset_frame.columnconfigure(0, weight=1)
        offset_frame.columnconfigure(1, weight=1)
        offset_frame.columnconfigure(2, weight=1)

        row += 1
        col = 0
        Label(frame, text=_("Step Size:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.step_size = tkExtra.FloatEntry(frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND)
        self.step_size.grid(row=row, column=col, sticky=EW)

        tkExtra.Balloon.set(
            self.step_size,
            _("Maximum XY distance between surface correction points.\n"
              "Smaller = smoother following of the surface (more points).\n"
              "Larger = fewer points, faster but less accurate.")
        )

        row += 1
        col = 0
        self.polynomial_degree_label = Label(frame, text=_("Poly Degree:"))
        self.polynomial_degree_label.grid(row=row, column=col, sticky=E)
        col += 1
        self.polynomial_degree = tkExtra.IntegerEntry(
            frame,
            background=tkExtra.GLOBAL_CONTROL_BACKGROUND)
        self.polynomial_degree.grid(row=row, column=col, sticky=EW)

        row += 1
        col = 0
        self.validation_status = Label(frame, text="", fg="gray", font=("TkDefaultFont", 8))
        self.validation_status.grid(row=row, column=col, columnspan=3, sticky=W)
        self.addWidget(self.validation_status)

        self.n_probe_points.bind('<KeyRelease>', self.update_validation_status)
        self.polynomial_degree.bind('<KeyRelease>', self.update_validation_status)

        row += 1;
        col = 0
        Button(frame, text=_("Generate Probe"), command=self.generate_probe).grid(row=row, column=col, sticky=W)
        col += 1
        Button(frame, text=_("Show Probe Points"), command=self.show_probe_points).grid(row=row, column=col, sticky=W)
        col += 1
        Button(frame, text=_("Start Probing"), command=self.start_probing).grid(row=row, column=col, sticky=W)

        row += 1;
        col = 0
        Button(frame, text=_("Select All"), command=lambda: self.app.event_generate("<<SelectAll>>")).grid(row=row,
                                                                                                           column=col,
                                                                                                           sticky=W)
        col += 1
        Button(frame, text=_("Surface Align G-Code"), command=self.surface_align_gcode).grid(row=row, column=col,
                                                                                             sticky=W)

        row += 1;
        col = 0
        Label(frame, text=_("Z Min Safety Limit:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.z_safety_limit = tkExtra.FloatEntry(frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND)
        self.z_safety_limit.grid(row=row, column=col, sticky=EW)

        # The whole-process buttons sit in `run_bar`, always on show
        if run_bar is None:
            run_bar = Frame(frame)
            run_bar.grid(row=row + 1, column=0, columnspan=3, sticky=EW)
        big = ("", 15, "bold")
        quick_align_run_b = Button(
            run_bar,
            text=_("▶  Align & Run"),
            command=self.quick_align_run,
            font=big, pady=10,
            bg="#4CAF50", fg="white",
            activebackground="#45a049", activeforeground="white"
        )
        quick_align_run_b.pack(side=LEFT, fill=X, expand=YES, padx=(0, 3))
        tkExtra.Balloon.set(
            quick_align_run_b,
            _("Align & Run\n\n"
              "Performs a complete surface alignment process:\n"
              "1. Generate G-code\n"
              "2. Generate probe points\n"
              "3. Start probing\n"
              "4. Select all points\n"
              "5. Surface align G-code\n"
              "6. Run the G-code\n\n"
              "Use this button to quickly align and run in one step.")
        )
        self.addWidget(quick_align_run_b)   # << auto-disable/enable during runs

        # Stop Running (with tooltip, NOT auto-managed — stays clickable)
        stop_run_b = Button(
            run_bar,
            text=_("■  Stop"),
            command=self.quick_align_stop,
            font=big, pady=10,
            bg="#F44336", fg="white",
            activebackground="#d32f2f", activeforeground="white"
        )
        stop_run_b.pack(side=LEFT, fill=X, expand=YES, padx=(3, 0))
        tkExtra.Balloon.set(
            stop_run_b,
            _("Immediately pauses/halts the current operation, retracts the probe, "
              "and cancels scheduled tasks. Use if you need to abort safely.")
        )

        frame.grid_columnconfigure(1, weight=1)
        self.loadConfig()
        self._on_probe_method_change()

    # ------------------ Config (unchanged) ------------------
    def loadConfig(self):
        self.mp_z_min.set(Utils.getFloat("SurfAlign", "mp_z_min"))
        self.mp_z_max.set(Utils.getFloat("SurfAlign", "mp_z_max"))
        self.n_probe_points.set(Utils.getInt("SurfAlign", "n_probe_points"))
        self.probe_coverage_method.set(Utils.getStr("SurfAlign", "probe_coverage_method"))
        self.bilin_grid_cell_size_mm.set(Utils.getFloat("SurfAlign", "bilin_grid_cell_size_mm"))
        self.x_probe_to_tool_offset.set(Utils.getFloat("SurfAlign", "x_probe_to_tool_offset"))
        self.y_probe_to_tool_offset.set(Utils.getFloat("SurfAlign", "y_probe_to_tool_offset"))
        self.z_probe_to_tool_offset.set(Utils.getFloat("SurfAlign", "z_probe_to_tool_offset"))
        self.z_safety_limit.set(Utils.getFloat("SurfAlign", "z_safety_limit"))
        self.step_size.set(Utils.getFloat("SurfAlign", "step_size"))
        self.polynomial_degree.set(Utils.getInt("SurfAlign", "polynomial_degree"))
        self._tool_tighten_error = Utils.getFloat("SurfAlign", "tool_tighten_error")
        self._toolcal_z_max = Utils.getFloat("SurfAlign", "toolcal_z_max", 5.0)
        self._toolcal_z_min = Utils.getFloat("SurfAlign", "toolcal_z_min", -10.0)
        self._toolcal_safe_height = Utils.getFloat("SurfAlign", "toolcal_safe_height", 5.0)
        self._toolcal_feed_rate = Utils.getFloat("SurfAlign", "toolcal_feed_rate", 50.0)
        self.update_validation_status()
        self._on_probe_method_change()

    def saveConfig(self):
        Utils.setFloat("SurfAlign", "mp_z_min", self.mp_z_min.get())
        Utils.setFloat("SurfAlign", "mp_z_max", self.mp_z_max.get())
        Utils.setInt("SurfAlign", "n_probe_points", self.n_probe_points.get())
        Utils.setStr("SurfAlign", "probe_coverage_method", self.probe_coverage_method.get())
        Utils.setFloat("SurfAlign", "bilin_grid_cell_size_mm", self.bilin_grid_cell_size_mm.get())
        Utils.setFloat("SurfAlign", "x_probe_to_tool_offset", self.x_probe_to_tool_offset.get())
        Utils.setFloat("SurfAlign", "y_probe_to_tool_offset", self.y_probe_to_tool_offset.get())
        Utils.setFloat("SurfAlign", "z_probe_to_tool_offset", self.z_probe_to_tool_offset.get())
        Utils.setFloat("SurfAlign", "z_safety_limit", self.z_safety_limit.get())
        Utils.setFloat("SurfAlign", "step_size", self.step_size.get())
        Utils.setInt("SurfAlign", "polynomial_degree", self.polynomial_degree.get())
        Utils.setFloat("SurfAlign", "tool_tighten_error", getattr(self, '_tool_tighten_error', 0.0))
        Utils.setFloat("SurfAlign", "toolcal_z_max", getattr(self, '_toolcal_z_max', 5.0))
        Utils.setFloat("SurfAlign", "toolcal_z_min", getattr(self, '_toolcal_z_min', -10.0))
        Utils.setFloat("SurfAlign", "toolcal_safe_height", getattr(self, '_toolcal_safe_height', 5.0))
        Utils.setFloat("SurfAlign", "toolcal_feed_rate", getattr(self, '_toolcal_feed_rate', 50.0))
        
    def _on_probe_method_change(self, event=None):
        """Show/hide widgets depending on selected probe coverage method."""
        method = self.probe_coverage_method_var.get()

        # Bilinear grid widgets (only for BilinearGrid)
        bilin_widgets = [
            self.bilin_grid_cell_label,
            self.bilin_grid_cell_size_mm,
        ]

        # Widgets used for polynomial-based methods (EvenCoverage / AreaCoverage)
        poly_widgets = [
            self.n_probe_points_label,
            self.n_probe_points,
            self.validation_status,
            self.polynomial_degree_label,
            self.polynomial_degree,
        ]

        if method == "BilinearGrid":
            # 👉 Bilinear: show grid cell size, hide No. of Points + Poly Degree
            for w in bilin_widgets:
                w.grid()          # restore previous grid position
            for w in poly_widgets:
                w.grid_remove()   # hide
        else:
            # 👉 Other methods: show No. of Points + Poly Degree, hide bilinear stuff
            for w in bilin_widgets:
                w.grid_remove()
            for w in poly_widgets:
                w.grid()


    # ------------------ Your existing logic, with minor tweaks ------------------
    def surface_align_gcode(self):
        self.app.gcode.x_probe_to_tool_offset = float(self.x_probe_to_tool_offset.get() or 0)
        self.app.gcode.y_probe_to_tool_offset = float(self.y_probe_to_tool_offset.get() or 0)
        self.app.gcode.z_probe_to_tool_offset = float(self.z_probe_to_tool_offset.get() or 0)

        no_of_points = int(self.n_probe_points.get())
        polynomial_degree = int(self.polynomial_degree.get())
        is_valid, message, _unused = self.validate_probe_points_vs_degree(no_of_points, polynomial_degree)
        if not is_valid:
            messagebox.showerror(_("Probe Configuration Error"), message)
            return False

        bounds = self.app.gcode.surf_align_gcode(self.app.editor.getSelectedBlocks(),
                                                 step_size=float(self.step_size.get()),
                                                 degree=polynomial_degree,
                                                 method=self.probe_coverage_method.get())
        self.app.drawAfter()

    def validate_probe_points_vs_degree(self, num_points, degree):
        min_points = (degree + 1) * (degree + 2) // 2
        ratio = num_points / max(min_points, 1)
        if num_points < min_points:
            msg = (f"❌ Insufficient probe points for degree {degree}.\n"
                   f"   Need ≥ {min_points}, got {num_points}.\n"
                   f"   Recommended: {int(min_points * 1.2)}+ for robustness.")
            return False, msg, int(min_points * 1.2)
        if ratio >= 1.2:
            return True, f"✅ Good: {num_points} points for degree {degree}.", None
        else:
            return True, f"⚠️ Low points for degree {degree}.", int(min_points * 1.2)

    def update_validation_status(self, event=None):
        try:
            num_points = int(self.n_probe_points.get())
            degree = int(self.polynomial_degree.get())
            is_valid, message, _unused = self.validate_probe_points_vs_degree(num_points, degree)
            if "❌" in message:
                self.validation_status.config(text=message.split('\n')[0], fg="red")
            elif "⚠️" in message:
                self.validation_status.config(text=message.split('\n')[0], fg="orange")
            else:
                self.validation_status.config(text=message.split('\n')[0], fg="green")
        except Exception:
            self.validation_status.config(text="", fg="gray")

    def _has_points(self):
        """True if probe_points is non-empty (list or numpy array)."""
        pts = getattr(self, "probe_points", None)
        if pts is None:
            return False
        # numpy arrays have .size; lists/tuples have len()
        size = getattr(pts, "size", None)
        if size is not None:
            return size > 0
        try:
            return len(pts) > 0
        except Exception:
            return False

    def _iter_points(self):
        """Iterate points safely (handles numpy arrays or lists)."""
        pts = getattr(self, "probe_points", None)
        if pts is None:
            return []
        tolist = getattr(pts, "tolist", None)
        if callable(tolist):
            return tolist()
        return list(pts)

    def set_tool_height_popup(self):
        """Show popup to guide user through physical tool height calibration."""
        dialog = Toplevel(self)
        dialog.title(_("Set Tool Height (Physical Calibration)"))
        dialog.geometry("400x370")
        dialog.resizable(False, False)
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.focus_set()
        
        # Center dialog
        dialog.geometry("+%d+%d" % (self.winfo_rootx() + 50, self.winfo_rooty() + 50))

        # Instructions
        instructions = [
            "1. Attach cutting bit LOOSELY (do not tighten yet)",
            "2. Click 'Start Calibration'",
            "3. Machine will run a probe at current location",
            "4. Machine will move to probe location and lower by Z offset",
            "5. Manually adjust loose cutting bit to touch the surface",
            "6. Tighten the cutting bit",
            "7. Done! Make minor adjustments to Z offset if needed"
        ]
        
        for i, instruction in enumerate(instructions):
            Label(dialog, text=_(instruction), justify=LEFT, anchor=W).pack(anchor=W, padx=10, pady=(10 if i == 0 else 0, 0))

        # Z offset info
        Label(dialog, text=_("\nCurrent Z Offset will be used to lower the tool."), 
              justify=LEFT, fg="blue", font=("TkDefaultFont", 9, "bold")).pack(anchor=W, padx=10, pady=0)

        # Z range inputs
        input_frame = Frame(dialog)
        input_frame.pack(fill=X, padx=10, pady=5)
        
        Label(input_frame, text=_("Z Max (Safe):")).grid(row=0, column=0, sticky=E)
        z_max_entry = tkExtra.FloatEntry(input_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=8)
        z_max_entry.grid(row=0, column=1, padx=5)
        z_max_entry.set(getattr(self, '_toolcal_z_max', 5.0))

        Label(input_frame, text=_("Z Min (Probe):")).grid(row=0, column=2, sticky=E)
        z_min_entry = tkExtra.FloatEntry(input_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=8)
        z_min_entry.grid(row=0, column=3, padx=5)
        z_min_entry.set(getattr(self, '_toolcal_z_min', -10.0))
        # Explanatory text
        Label(dialog, text=_("Z values are relative to the current\nmachine position (for probe range)."), 
              justify=LEFT, fg="gray", font=("TkDefaultFont", 8)).pack(anchor=W, padx=10, pady=(0, 5))

        # Loose tool controls
        tool_frame = Frame(dialog)
        tool_frame.pack(fill=X, padx=10, pady=5)
        
        Label(tool_frame, text=_("Loose Tool Safe Z:")).grid(row=0, column=0, sticky=E)
        safe_height_entry = tkExtra.FloatEntry(tool_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=8)
        safe_height_entry.grid(row=0, column=1, padx=5)
        safe_height_entry.set(getattr(self, '_toolcal_safe_height', 5.0))
        tkExtra.Balloon.set(safe_height_entry, _("Safe clearance for moving loose tool, relative to tool position"))
        
        Label(tool_frame, text=_("Descent Feed Rate:")).grid(row=0, column=2, sticky=E)
        feed_rate_entry = tkExtra.FloatEntry(tool_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=8)
        feed_rate_entry.grid(row=0, column=3, padx=5)
        feed_rate_entry.set(getattr(self, '_toolcal_feed_rate', 50.0))
        tkExtra.Balloon.set(feed_rate_entry, _("Slow feed rate for lowering loose tool (mm/min)"))
        
        Label(tool_frame, text=_("Tighten Error:")).grid(row=1, column=0, sticky=E)
        tighten_error_entry = tkExtra.FloatEntry(tool_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=8)
        tighten_error_entry.grid(row=1, column=1, padx=5)
        tighten_error_entry.set(getattr(self, '_tool_tighten_error', 0.0))  # Load saved value
        tkExtra.Balloon.set(tighten_error_entry, _("Distance the tool shifts up when tightened (mm).\nTool will be raised above target by this amount to account for tightening."))

        btn_frame = Frame(dialog)
        btn_frame.pack(fill=X, padx=10, pady=15)

        Button(btn_frame, text=_("Cancel"), command=dialog.destroy).pack(side=RIGHT)
        Button(btn_frame, text=_("Start Calibration"), command=lambda: self.run_set_tool_height(dialog, z_max_entry, z_min_entry, safe_height_entry, feed_rate_entry, tighten_error_entry), 
               bg="#4CAF50", fg="white").pack(side=RIGHT, padx=10)

    def run_set_tool_height(self, dialog, z_max_entry, z_min_entry, safe_height_entry, feed_rate_entry, tighten_error_entry):
        """Execute the tool height calibration sequence."""
        try:
            user_z_max = float(z_max_entry.get())
            user_z_min = float(z_min_entry.get())
            user_safe_height = float(safe_height_entry.get())
            user_feed_rate = float(feed_rate_entry.get())
            user_tighten_error = float(tighten_error_entry.get())
        except ValueError:
            messagebox.showerror(_("Error"), _("Invalid input values"))
            return
            
        dialog.destroy()
        
        # Get current position and offsets
        try:
            start_wx = CNC.vars["wx"]
            start_wy = CNC.vars["wy"]
            start_wz = CNC.vars["wz"]
        except KeyError:
            messagebox.showerror(_("Error"), _("Machine position not available."))
            return


        print(f"Set Tool Height: WCS=({start_wx}, {start_wy}, {start_wz})")

        # Setup probe sequence - single point at current location
        probe_points = [[start_wx, start_wy]]
        
        # Use user-provided probe range (relative to current position)
        mp_z_min = start_wz + user_z_min
        mp_z_max = start_wz + user_z_max
        
        # Store original xmin/ymin
        original_xmin = self.app.gcode.probe.xmin
        original_ymin = self.app.gcode.probe.ymin
        self.app.gcode.probe.xmin = start_wx
        self.app.gcode.probe.ymin = start_wy

        # Store parameters for the delayed callback
        self._calibration_original_xmin = original_xmin
        self._calibration_original_ymin = original_ymin
        self._calibration_probe_points = probe_points
        self._calibration_mp_z_min = mp_z_min
        self._calibration_mp_z_max = mp_z_max
        self._safe_height_for_loose_tool = user_safe_height
        self._loose_tool_descent_feed_rate = user_feed_rate
        self._tool_tighten_error = user_tighten_error
        self._toolcal_z_max = user_z_max
        self._toolcal_z_min = user_z_min
        self._toolcal_safe_height = user_safe_height
        self._toolcal_feed_rate = user_feed_rate
        # Save all popup values immediately
        Utils.setFloat("SurfAlign", "tool_tighten_error", user_tighten_error)
        Utils.setFloat("SurfAlign", "toolcal_z_max", user_z_max)
        Utils.setFloat("SurfAlign", "toolcal_z_min", user_z_min)
        Utils.setFloat("SurfAlign", "toolcal_safe_height", user_safe_height)
        Utils.setFloat("SurfAlign", "toolcal_feed_rate", user_feed_rate)

        # Move Z-up before deploying probe (outside callback so it has time to complete)
        try:
            safe_z = mp_z_max
            if hasattr(self.app, "mcontrol") and hasattr(self.app.mcontrol, "jog"):
                self.app.mcontrol.jog(f"Z{safe_z:.4f}")
                print(f"[SET_TOOL_HEIGHT] Jog Z to safe height: {safe_z:.4f}")
        except Exception as e:
            print(f"[SET_TOOL_HEIGHT] Jog Z before deploy error: {e}")

        # Deploy probe and run with delay (similar to _deploy_and_run pattern)
        def _deploy_and_run_calibration():
            try:
                # Deploy probe
                self.app.blt_serial_send('1')
                self.probe_deployed = True
                print("[SET_TOOL_HEIGHT] Probe deployed")
            except Exception as e:
                print(f"[SET_TOOL_HEIGHT] Deploy probe error: {e}")

            try:
                # Generate probe sequence
                # Pass 0 for x_off and y_off so probe happens at current XY position
                lines = self.app.gcode.probe.multi_point_scan(
                    self._calibration_probe_points, 
                    self._calibration_mp_z_min, 
                    self._calibration_mp_z_max, 
                    0.0,  # No X offset - probe at current position
                    0.0,  # No Y offset - probe at current position
                    0.0   # No Z offset for probing
                )
                
                self.app.run(lines)
                
                # Restore xmin/ymin
                self.app.gcode.probe.xmin = self._calibration_original_xmin
                self.app.gcode.probe.ymin = self._calibration_original_ymin

                # Wait for probe to complete, then adjust Z position
                self._calibration_poll_count = 0
                self.app.after(1000, self._poll_calibration_complete)

            except Exception as e:
                self.app.gcode.probe.xmin = self._calibration_original_xmin
                self.app.gcode.probe.ymin = self._calibration_original_ymin
                self._retract_probe("[SET_TOOL_HEIGHT_ERROR]")
                messagebox.showerror(_("Error"), f"Failed to run probe: {e}")

        # Delay deploy to allow Z movement to complete
        self.app.after(500, _deploy_and_run_calibration)

    def _poll_calibration_complete(self):
        """Poll for probe completion, then move tool to calibration position."""
        # Check if probe is still running
        if self.app.running or self.app.gcode.probe.is_multi_point_scan:
            if self._calibration_poll_count < 300:  # 30 second timeout
                self._calibration_poll_count += 1
                self.app.after(100, self._poll_calibration_complete)
                return
            else:
                messagebox.showerror(_("Error"), _("Timeout waiting for probe."))
                self.app.gcode.probe.is_multi_point_scan = False
                self._retract_probe("[CALIBRATION_TIMEOUT]")
                return

        # Check for alarm
        state = CNC.vars.get("state", "Idle")
        if state == "Alarm":
            self._retract_probe("[CALIBRATION_ALARM]")
            messagebox.showerror(_("Error"), _("Probing failed (Alarm state)."))
            return

        # Retract probe after successful completion
        self._retract_probe("[CALIBRATION_COMPLETE]")

        # Get probe results
        results = self.app.gcode.probe.multi_probe_points
        if results and len(results) > 0:
            probe_point = results[-1]
            probe_x, probe_y, probe_z = probe_point[0], probe_point[1], probe_point[2]
            
            print(f"Probe result: ({probe_x}, {probe_y}, {probe_z})")
            
            # Get XYZ offsets from UI
            try:
                x_off = float(self.x_probe_to_tool_offset.get() or 0)
                y_off = float(self.y_probe_to_tool_offset.get() or 0)
                z_off = float(self.z_probe_to_tool_offset.get() or 0)
            except ValueError:
                x_off, y_off, z_off = 0.0, 0.0, 0.0
            
            # Calculate target position: tool needs to be at the same physical location as probe
            # Since probe is offset from tool, we subtract the offsets
            target_x = probe_x - x_off
            target_y = probe_y - y_off
            target_z = probe_z - z_off
            
            # Account for tighten error: tool shifts up when tightened,
            # so position tool above target by this amount
            tighten_error = getattr(self, '_tool_tighten_error', 0.0)
            target_z = target_z + tighten_error
            
            # Generate movement commands
            move_commands = []
            move_commands.append(f"G90")  # Absolute positioning
            # Use the safe height from popup
            safe_height = getattr(self, '_safe_height_for_loose_tool', 5.0)
            feed_rate = getattr(self, '_loose_tool_descent_feed_rate', 50.0)
            move_commands.append(f"G0 Z{target_z + safe_height:.4f}")  # Lift to safe height first
            move_commands.append(f"G0 X{target_x:.4f} Y{target_y:.4f}")  # Move to target XY
            move_commands.append(f"G1 Z{target_z:.4f} F{feed_rate:.1f}")  # Lower slowly at specified feed rate
            
            print(f"[SET_TOOL_HEIGHT] Sending move commands: {move_commands}")
            
            # Store info for the completion message
            self._tool_height_target = (target_x, target_y, target_z)

            # Defer the run() call so the previous probe run state fully clears
            def _run_move_commands():
                if self.app.running:
                    # Still running from probe — retry shortly
                    print("[SET_TOOL_HEIGHT] App still running, retrying in 500ms...")
                    self.app.after(500, _run_move_commands)
                    return
                try:
                    self.app.run(move_commands)
                    print("[SET_TOOL_HEIGHT] Move commands sent successfully")
                except Exception as e:
                    print(f"[SET_TOOL_HEIGHT] Move command error: {e}")
                    messagebox.showerror(_("Error"), f"Failed to move tool: {e}")
                    return

                # Poll for movement completion, then show the info dialog
                self._tool_move_poll_count = 0
                self.app.after(500, self._poll_tool_move_complete)

            self.app.after(500, _run_move_commands)
        else:
            messagebox.showerror(_("Error"), _("No probe points recorded."))

    def _poll_tool_move_complete(self):
        """Poll until the tool movement commands finish, then show the info dialog."""
        if self.app.running:
            if self._tool_move_poll_count < 300:  # 30 second timeout
                self._tool_move_poll_count += 1
                self.app.after(100, self._poll_tool_move_complete)
                return
            else:
                messagebox.showwarning(_("Warning"), _("Timeout waiting for tool movement to finish."))
                return

        target_x, target_y, target_z = self._tool_height_target
        messagebox.showinfo(_("Set Tool Height"), 
                          _("Tool moved to probe location.\n\n"
                            "Machine compensated for XYZ offset.\n"
                            "Adjust the loose cutting bit to touch the surface,\n"
                            "then tighten it.\n\n"
                            f"Target: X{target_x:.4f} Y{target_y:.4f} Z{target_z:.4f}"))

    def measure_z_offset_popup(self, target_entry=None):
        """Show popup to guide user for Z offset measurement.

        Args:
            target_entry: Optional widget whose .set() receives the result.
                          When None (default) the result updates the global
                          z_probe_to_tool_offset field as usual.
        """
        dialog = Toplevel(self)
        dialog.title(_("Measure Z Offset"))
        if target_entry is not None:
            dialog.geometry("350x410")
        else:
            dialog.geometry("350x370")
        dialog.resizable(True, True)
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.focus_set()

        # Center dialog
        dialog.geometry("+%d+%d" % (self.winfo_rootx() + 50, self.winfo_rooty() + 50))

        Label(dialog, text=_("1. Jog tool to touch a point on the surface of the lid."), justify=LEFT).pack(anchor=W, padx=10, pady=(10, 5))
        Label(dialog, text=_("2. Click 'Measure'."), justify=LEFT).pack(anchor=W, padx=10, pady=5)
        Label(dialog, text=_("Machine will lift by Safe Lift, move to the XY Offset position,\nand probe down by Probe Depth."), justify=LEFT, fg="gray").pack(anchor=W, padx=10, pady=5)

        # When called from the calibrate popup, show a note about where result goes
        if target_entry is not None:
            Label(dialog, text=_("Result will be placed into 'Z Offset (Probe \u2192 Tip)' only.\nIt will NOT be saved to the main window."),
                  justify=LEFT, fg="#1565C0", font=("TkDefaultFont", 8)).pack(anchor=W, padx=10, pady=(0, 3))

        # Min/Max Z Inputs — grouped in a LabelFrame
        z_group = LabelFrame(dialog, text=_("Probe Range"), padx=6, pady=4)
        z_group.pack(fill=X, padx=10, pady=5)

        input_frame = Frame(z_group)
        input_frame.pack(fill=X)

        Label(input_frame, text=_("Safe Lift:")).grid(row=0, column=0, sticky=E)
        z_max_entry = tkExtra.FloatEntry(input_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=8)
        z_max_entry.grid(row=0, column=1, padx=5)
        z_max_entry.set(5.0) # Default relative lift (positive = upward)
        tkExtra.Balloon.set(z_max_entry, _("Distance (mm) to lift the probe above the surface contact point\nbefore moving to the probe XY location. Absolute value is used."))
        Label(input_frame, text=_("mm"), fg="gray", font=("TkDefaultFont", 8)).grid(row=1, column=1, sticky=W, padx=5)

        Label(input_frame, text=_("Probe Depth:")).grid(row=0, column=2, sticky=E)
        z_min_entry = tkExtra.FloatEntry(input_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=8)
        z_min_entry.grid(row=0, column=3, padx=5)
        z_min_entry.set(10.0) # Default relative probe depth (positive = downward)
        tkExtra.Balloon.set(z_min_entry, _("Distance (mm) to descend below the surface contact point\nwhen probing. Absolute value is used."))
        Label(input_frame, text=_("mm"), fg="gray", font=("TkDefaultFont", 8)).grid(row=1, column=3, sticky=W, padx=5)

        # Explanatory text — grid row 2
        Label(input_frame, text=_("Both fields above are relative to the current tool position\n(surface contact point). Absolute value is used for each."),
              justify=LEFT, fg="gray", font=("TkDefaultFont", 8)).grid(row=2, column=0, columnspan=4, sticky=W, padx=2, pady=(4, 2))

        # Local XY offset fields — separate group outside Probe Range
        xy_group = LabelFrame(dialog, text=_("XY Offset (local — not saved)"), padx=6, pady=4)
        xy_group.pack(fill=X, padx=10, pady=(0, 5))

        xy_frame = Frame(xy_group)
        xy_frame.pack(fill=X)

        Label(xy_frame, text=_("X Offset:")).grid(row=0, column=0, sticky=E, pady=2)
        local_x_offset = tkExtra.FloatEntry(xy_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=8)
        local_x_offset.grid(row=0, column=1, padx=5, pady=2)
        local_x_offset.set(self.x_probe_to_tool_offset.get() or "0")
        tkExtra.Balloon.set(local_x_offset, _("X distance from tool tip to probe tip.\nLoaded from main window — editable here for this measurement only.\nChanges will NOT be saved back to the main window."))

        Label(xy_frame, text=_("Y Offset:")).grid(row=0, column=2, sticky=E, pady=2)
        local_y_offset = tkExtra.FloatEntry(xy_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=8)
        local_y_offset.grid(row=0, column=3, padx=5, pady=2)
        local_y_offset.set(self.y_probe_to_tool_offset.get() or "0")
        tkExtra.Balloon.set(local_y_offset, _("Y distance from tool tip to probe tip.\nLoaded from main window — editable here for this measurement only.\nChanges will NOT be saved back to the main window."))

        Label(xy_frame, text=_("mm"), fg="gray", font=("TkDefaultFont", 8)).grid(row=1, column=1, sticky=W, padx=5)


        Label(xy_frame, text=_("mm"), fg="gray", font=("TkDefaultFont", 8)).grid(row=1, column=3, sticky=W, padx=5)

        btn_frame = Frame(dialog)
        btn_frame.pack(fill=X, padx=10, pady=10)

        Button(btn_frame, text=_("Cancel"), command=dialog.destroy).pack(side=RIGHT)
        Button(btn_frame, text=_("Measure"),
               command=lambda: self.run_measure_z(dialog, z_max_entry, z_min_entry,
                                                  target_entry=target_entry,
                                                  local_x_offset=local_x_offset,
                                                  local_y_offset=local_y_offset),
               bg="#4CAF50", fg="white").pack(side=RIGHT, padx=10)

    def run_measure_z(self, dialog, z_max_entry, z_min_entry, target_entry=None,
                      local_x_offset=None, local_y_offset=None):
        """Execute the Z offset measurement sequence using multi_point_scan.

        Args:
            target_entry:    Optional widget to receive the result instead of
                             the global z_probe_to_tool_offset field.
            local_x_offset:  Optional local-only FloatEntry for X probe offset.
                             When supplied, its value is used instead of the
                             global x_probe_to_tool_offset field (not written back).
            local_y_offset:  Same as local_x_offset but for Y.
        """
        try:
            user_z_max = float(z_max_entry.get())
            user_z_min = float(z_min_entry.get())
            
        except ValueError:
            messagebox.showerror(_("Error"), _("Invalid Z Min/Max values"))
            return

        dialog.destroy()
        
        # 0. Ensure probe is retracted before any movement
        self._retract_probe("[MEASURE_Z]")

        # 1. Record current Machine Z (Tool Z) and store optional target
        try:
            self._measure_start_mz = CNC.vars["mz"]
            start_wx = CNC.vars["wx"]
            start_wy = CNC.vars["wy"]
            start_wz = CNC.vars["wz"]
        except KeyError:
            messagebox.showerror(_("Error"), _("Machine position not available."))
            return

        # Remember where to write the result
        self._measure_z_target_entry = target_entry

        # 2. Get XY Offsets – prefer local popup entries (not saved back to main window)
        try:
            x_val = local_x_offset.get().strip()
            y_val = local_y_offset.get().strip()
            if not x_val or not y_val:
                messagebox.showerror(_("Error"), _("X and Y Offsets are required. Please enter values or cancel."))
                return
            x_off = float(x_val)
            y_off = float(y_val)
            # Z Offset ignored as per instruction: "pass it as zero"
            z_off_field = 0.0 
        except (ValueError, AttributeError):
            messagebox.showerror(_("Error"), _("Invalid or missing Offset Values"))
            return

        print(f"Measure Z: WCS=({start_wx}, {start_wy}, {start_wz}), OffsetXY=({x_off}, {y_off})")

        # 3. Setup arguments for multi_point_scan
        probe_points = [[start_wx, start_wy]]

        # Z depths – apply user relative inputs to current WZ
        mp_z_min = start_wz - abs(user_z_min)  # depth is positive; subtract to go downward
        mp_z_max = start_wz + abs(user_z_max)  # lift is positive; add to go upward
        
        # 4. Handle xmin/ymin side-effect of multi_point_scan
        original_xmin = self.app.gcode.probe.xmin
        original_ymin = self.app.gcode.probe.ymin
        self.app.gcode.probe.xmin = start_wx
        self.app.gcode.probe.ymin = start_wy

        # 5. Move Z-up before deploying probe
        try:
            if hasattr(self.app, "mcontrol") and hasattr(self.app.mcontrol, "jog"):
                self.app.mcontrol.jog(f"Z{mp_z_max:.4f}")
                print(f"[MEASURE_Z] Jog Z to safe height: {mp_z_max:.4f}")
        except Exception as e:
            print(f"[MEASURE_Z] Jog Z before deploy error: {e}")

        def _deploy_and_run_measure():
            try:
                # Deploy probe
                self.app.blt_serial_send('1')
                self.probe_deployed = True
                print("[MEASURE_Z] Probe deployed")
            except Exception as e:
                print(f"[MEASURE_Z] Deploy probe error: {e}")

            try:
                lines = self.app.gcode.probe.multi_point_scan(
                    probe_points, 
                    mp_z_min, 
                    mp_z_max, 
                    x_off, 
                    y_off, 
                    z_off_field
                )
                self.app.run(lines)
                
                self.app.gcode.probe.xmin = original_xmin
                self.app.gcode.probe.ymin = original_ymin

                # 6. Schedule Polling
                self._measure_poll_count = 0
                self.app.after(1000, self._poll_measure_z)

            except Exception as e:
                self.app.gcode.probe.xmin = original_xmin
                self.app.gcode.probe.ymin = original_ymin
                self._retract_probe("[MEASURE_Z_ERROR]")
                messagebox.showerror(_("Error"), f"Failed to generate probe command: {e}")

        # Delay deploy to allow Z movement to complete
        self.app.after(500, _deploy_and_run_measure)


    def _poll_measure_z(self):
        """Check if probing finished (is_multi_point_scan became False).

        Writes the calculated offset to self._measure_z_target_entry when it
        is set (caller-supplied widget), otherwise writes to the global
        z_probe_to_tool_offset field.
        """
        # Polling: Check if busy or Probe is still in scanning mode
        # Probe class sets is_multi_point_scan=False when finished.
        if self.app.running or self.app.gcode.probe.is_multi_point_scan:
            if self._measure_poll_count < 1200: # 120 seconds timeout
                self._measure_poll_count += 1
                self.app.after(100, self._poll_measure_z)
                return
            else:
                messagebox.showerror(_("Error"), _("Timeout waiting for probe after 2 minutes."))
                self.app.gcode.probe.is_multi_point_scan = False
                return

        # Check for Alarm
        state = CNC.vars.get("state", "Idle")
        if state == "Alarm":
            messagebox.showerror(_("Error"), _("Probing failed (Alarm state active)."))
            return

        # Check results in multi_probe_points
        results = self.app.gcode.probe.multi_probe_points
        if results and len(results) > 0:
            # Last point is the one we want (we only did one). Result is [x, y, z].
            prb_z = results[-1][2]

            # Offset = ProbeZ - ToolZ (ToolZ recorded at start)
            z_offset = prb_z - self._measure_start_mz

            target = getattr(self, "_measure_z_target_entry", None)
            if target is not None:
                # Write into the caller-supplied entry (e.g. tip_offset_entry in calibrate popup)
                try:
                    target.set(f"{z_offset:.4f}")
                except Exception:
                    pass
                messagebox.showinfo(
                    _("Measure Z"),
                    _(f"Captured Offset: {z_offset:.4f}\n"
                      "(Written to 'Z Offset (Probe \u2192 Tip)' \u2014 not saved to main window.)")
                )
            else:
                # Normal path: update the global field
                self.z_probe_to_tool_offset.set(f"{z_offset:.4f}")
                messagebox.showinfo(_("Measure Z"), _(f"Captured Offset: {z_offset:.4f}\n(Updated Field)"))
        else:
            print("Measure Z: No probe points recorded.")

    def measure_z_offset_bitsetter_popup(self):
        """Show popup to guide user for Z offset measurement using Bitsetter."""
        dialog = Toplevel(self)
        dialog.title(_("Measure Z Offset (Bitsetter)"))
        dialog.resizable(True, True)
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        dialog.focus_set()
        
        # Center dialog
        dialog.geometry("+%d+%d" % (self.winfo_rootx() + 50, self.winfo_rooty() + 50))
        
        input_frame = Frame(dialog)
        input_frame.pack(fill=X, padx=10, pady=5)
        
        # Row 0 – Probe Start X
        Label(input_frame, text=_("Probe Start X:")).grid(row=0, column=0, sticky=E, pady=(0, 2))
        homing_x_entry = tkExtra.FloatEntry(input_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=12)
        homing_x_entry.grid(row=0, column=1, padx=5, pady=(0, 2), sticky=W)
        homing_x_entry.set(Utils.getStr("SurfAlign", "bitsetter_x", ""))
        tkExtra.Balloon.set(homing_x_entry, _("Machine X coordinate to jog to before probing (Bitsetter position)"))

        # Row 1 – Probe Start Z
        Label(input_frame, text=_("Probe Start Z:")).grid(row=1, column=0, sticky=E, pady=(0, 2))
        homing_z_entry = tkExtra.FloatEntry(input_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=12)
        homing_z_entry.grid(row=1, column=1, padx=5, pady=(0, 2), sticky=W)
        homing_z_entry.set(Utils.getStr("SurfAlign", "bitsetter_z", ""))
        tkExtra.Balloon.set(homing_z_entry, _("Machine Z safe height to jog to before probing"))

        # Row 2 – Probe Depth
        Label(input_frame, text=_("Probe Depth:")).grid(row=2, column=0, sticky=E, pady=(10, 2))
        probe_depth_entry = tkExtra.FloatEntry(input_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=15)
        probe_depth_entry.grid(row=2, column=1, padx=5, pady=(10, 2))
        probe_depth_entry.set(Utils.getFloat("SurfAlign", "bitsetter_probe_depth", 10.0))
        tkExtra.Balloon.set(probe_depth_entry, _("How far (mm) to descend from the Probe Start Z position"))

        # Explanatory text on the right
        Label(input_frame, text=_("(mm downward from\nProbe Start Z)"),
              justify=LEFT, fg="gray", font=("TkDefaultFont", 8)).grid(row=2, column=2, sticky=W, padx=5)

        # Row 3 – Home First checkbox
        home_first_var = IntVar()
        home_first_var.set(Utils.getInt("SurfAlign", "bitsetter_home_first", 1))
        Checkbutton(input_frame, text=_("Home ($H) first"), variable=home_first_var).grid(row=3, column=1, sticky=W, pady=(5, 0))

        # Row 4: Calibrated BLTouch Pos + Calibrate button that opens a separate window
        Label(input_frame, text=_("Calibrated BLTouch Pos (Z):")).grid(row=4, column=0, sticky=E)
        bltouch_z_entry = tkExtra.FloatEntry(input_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=15)
        bltouch_z_entry.grid(row=4, column=1, padx=5, pady=2)
        bltouch_z_entry.set(Utils.getFloat("SurfAlign", "bitsetter_bltouch_z", -60.0))
        tkExtra.Balloon.set(bltouch_z_entry, _("Recorded Machine Z value of the calibrated BLTouch position touching the bitsetter."))
        Button(
            input_frame,
            text=_("Calibrate"),
            command=lambda: self.open_calibrate_bltouch_z_popup(
                homing_x_entry, homing_z_entry, probe_depth_entry,
                feed_rate_entry, home_first_var, bltouch_z_entry
            ),
            bg="#2196F3", fg="white"
        ).grid(row=4, column=2, padx=5, pady=2, sticky=W)

        # Row 5: Probe Feed Rate
        Label(input_frame, text=_("Probe Feed Rate:")).grid(row=5, column=0, sticky=E)
        feed_rate_entry = tkExtra.FloatEntry(input_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=15)
        feed_rate_entry.grid(row=5, column=1, padx=5, pady=2)
        feed_rate_entry.set(Utils.getFloat("SurfAlign", "bitsetter_feed_rate", 50.0))

        Button(input_frame, text=_("Measure"), command=lambda: self.run_measure_z_bitsetter(dialog, homing_x_entry, homing_z_entry, probe_depth_entry, bltouch_z_entry, feed_rate_entry, home_first_var), bg="#4CAF50", fg="white").grid(row=6, column=1, sticky=E, padx=5, pady=10)
        Button(input_frame, text=_("Cancel"), command=dialog.destroy).grid(row=6, column=2, sticky=W, padx=5, pady=10)

    def open_calibrate_bltouch_z_popup(self, homing_x_entry, homing_z_entry, probe_depth_entry,
                                        feed_rate_entry, home_first_var, bltouch_z_entry):
        """Open a small dedicated window for calibrating the BLTouch Z position.

        All fields are pre-filled from the parent dialog and are editable here,
        but changes are LOCAL to this popup only — they do not propagate back to
        the main window and are not saved to config.
        """
        win = Toplevel(self)
        win.title(_("Calibrate BLTouch Z"))
        win.resizable(False, False)
        win.transient(self.winfo_toplevel())
        win.grab_set()
        win.focus_set()
        win.geometry("+%d+%d" % (self.winfo_rootx() + 80, self.winfo_rooty() + 80))

        # ── Info banner ───────────────────────────────────────────────────────
        info_frame = Frame(win, bd=1, relief="solid", bg="#FFF9C4")
        info_frame.pack(fill=X, padx=15, pady=(10, 4))
        Label(
            info_frame,
            text=_("ℹ  Values below are loaded from the parent dialog.\n"
                   "   You may edit them here to verify before measuring.\n"
                   "   Changes will NOT be saved back to the main window."),
            justify=LEFT, fg="#5D4037", bg="#FFF9C4",
            font=("TkDefaultFont", 8),
        ).pack(anchor=W, padx=6, pady=4)

        # ── Main fields ───────────────────────────────────────────────────────
        f = Frame(win)
        f.pack(fill=X, padx=15, pady=6)

        col_lbl = {"sticky": "E", "padx": (0, 4)}
        col_ent = {"sticky": "W", "padx": (0, 8), "pady": 3}

        # Row 0 – Probe Start X
        Label(f, text=_("Probe Start X:")).grid(row=0, column=0, **col_lbl)
        local_homing_x = tkExtra.FloatEntry(f, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=12)
        local_homing_x.grid(row=0, column=1, **col_ent)
        local_homing_x.set(homing_x_entry.get())
        tkExtra.Balloon.set(local_homing_x, _("Machine X coordinate to jog to before probing (local copy — not saved)."))

        # Row 1 – Probe Start Z
        Label(f, text=_("Probe Start Z:")).grid(row=1, column=0, **col_lbl)
        local_homing_z = tkExtra.FloatEntry(f, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=12)
        local_homing_z.grid(row=1, column=1, **col_ent)
        local_homing_z.set(homing_z_entry.get())
        tkExtra.Balloon.set(local_homing_z, _("Machine Z safe height to jog to before probing (local copy — not saved)."))

        # Row 2 – Probe Depth
        Label(f, text=_("Probe Depth (mm):")).grid(row=2, column=0, **col_lbl)
        local_probe_depth = tkExtra.FloatEntry(f, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=12)
        local_probe_depth.grid(row=2, column=1, **col_ent)
        local_probe_depth.set(probe_depth_entry.get())
        tkExtra.Balloon.set(local_probe_depth, _("How far to descend from Probe Start Z during the calibration probe (local copy — not saved)."))

        # Row 3 – Feed Rate
        Label(f, text=_("Probe Feed Rate (mm/min):")).grid(row=3, column=0, **col_lbl)
        local_feed_rate = tkExtra.FloatEntry(f, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=12)
        local_feed_rate.grid(row=3, column=1, **col_ent)
        local_feed_rate.set(feed_rate_entry.get())
        tkExtra.Balloon.set(local_feed_rate, _("Feed rate for the calibration probe move (local copy — not saved)."))

        # Row 4 – Home First checkbox
        local_home_first_var = IntVar(value=home_first_var.get())
        Checkbutton(
            f, text=_("Home ($H) first"),
            variable=local_home_first_var,
        ).grid(row=4, column=1, sticky=W, pady=(2, 6))


        # ── Separator ─────────────────────────────────────────────────────────
        Frame(win, height=1, bd=0, relief="flat", bg="#BDBDBD").pack(fill=X, padx=15, pady=(0, 6))

        # Row 6 – Z Offset Probe→Tip (the primary editable field)
        f2 = Frame(win)
        f2.pack(fill=X, padx=15)
        Label(f2, text=_("Z Offset (Probe → Tip):")).grid(row=0, column=0, sticky=E, padx=(0, 4))
        tip_offset_entry = tkExtra.FloatEntry(f2, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=12)
        tip_offset_entry.grid(row=0, column=1, sticky=W, pady=4)
        tip_offset_entry.set(Utils.getFloat("SurfAlign", "bitsetter_bltouch_tip_offset", 0.0))
        tkExtra.Balloon.set(
            tip_offset_entry,
            _("Z distance from BLTouch probe to tool tip. The absolute value will be used.")
        )
        Label(f2, text=_("(abs. value used)"), fg="gray", font=("TkDefaultFont", 8)).grid(
            row=0, column=2, sticky=W, padx=4)
        # Button re-uses measure_z_offset_popup; result goes into tip_offset_entry only
        Button(
            f2,
            text=_("Measure Z Offset\u2026"),
            command=lambda: self.measure_z_offset_popup(target_entry=tip_offset_entry),
            bg="#4CAF50", fg="white", font=("TkDefaultFont", 8),
        ).grid(row=1, column=0, columnspan=3, sticky=W, pady=(0, 4))

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_frame = Frame(win)
        btn_frame.pack(fill=X, padx=15, pady=(8, 12))
        Button(
            btn_frame,
            text=_("Take Measurement"),
            command=lambda: self.run_measure_bltouch_cal_z(
                local_homing_x, local_homing_z, local_probe_depth,
                local_feed_rate, local_home_first_var,
                tip_offset_entry, bltouch_z_entry
            ),
            bg="#2196F3", fg="white"
        ).pack(side=LEFT)
        Button(btn_frame, text=_("Close"), command=win.destroy).pack(side=RIGHT)

    def run_measure_bltouch_cal_z(self, homing_x_entry, homing_z_entry, probe_depth_entry,
                                   feed_rate_entry, home_first_var,
                                   bltouch_tip_offset_entry, bltouch_z_entry):
        """Jog to bitsetter XZ, probe it, then set Calibrated BLTouch Pos = probed_z + abs(tip_offset)."""
        # Validate entries
        try:
            bs_x = float(homing_x_entry.get().strip())
        except (ValueError, AttributeError):
            messagebox.showerror(_("Error"), _("Probe Start X position is required. Please fill in the Probe start Pos (X,Z) fields."))
            return
        try:
            bs_z = float(homing_z_entry.get().strip())
        except (ValueError, AttributeError):
            messagebox.showerror(_("Error"), _("Probe Start Z position is required. Please fill in the Probe start Pos (X,Z) fields."))
            return
        try:
            probe_depth = float(probe_depth_entry.get())
            feed_rate   = float(feed_rate_entry.get())
            tip_offset  = abs(float(bltouch_tip_offset_entry.get()))
            home_first  = home_first_var.get()
        except ValueError:
            messagebox.showerror(_("Error"), _("Invalid numeric values"))
            return

        # Save tip offset to config
        Utils.setFloat("SurfAlign", "bitsetter_bltouch_tip_offset", float(bltouch_tip_offset_entry.get()))

        # Retract probe before motion
        self._retract_probe("[CALIBRATE_BLTOUCH_Z]")

        wcs_z_min = bs_z - abs(probe_depth)

        original_xmin = self.app.gcode.probe.xmin
        original_ymin = self.app.gcode.probe.ymin

        try:
            start_wy = CNC.vars["wy"]
        except KeyError:
            messagebox.showerror(_("Error"), _("Machine position not available."))
            return

        try:
            jog_lines = []
            if home_first:
                jog_lines.append("$H")
            else:
                jog_lines.append("G53 G0 Z0")
            jog_lines.append(f"G53 G0 X{bs_x:.4f}")
            jog_lines.append(f"G53 G0 Z{bs_z:.4f}")

            probe_points = [[bs_x, start_wy]]
            self.app.gcode.probe.xmin = min(original_xmin, bs_x)
            self.app.gcode.probe.ymin = min(original_ymin, start_wy)

            original_prbfeed = CNC.vars.get("prbfeed", 50.0)
            CNC.vars["prbfeed"] = feed_rate

            scan_lines = self.app.gcode.probe.multi_point_scan(
                probe_points,
                wcs_z_min,   # end_z
                bs_z,        # start_z
                0.0, 0.0, 0.0
            )

            CNC.vars["prbfeed"] = original_prbfeed

            full_program = jog_lines + scan_lines
            self.app.run(full_program)

            self.app.gcode.probe.xmin = original_xmin
            self.app.gcode.probe.ymin = original_ymin

            # Store the tip_offset so the poll callback can use it
            self._cal_blt_tip_offset = tip_offset
            self._cal_blt_z_entry    = bltouch_z_entry
            self._cal_blt_poll_count = 0
            self.app.after(1000, self._poll_cal_bltouch_z)

        except Exception as e:
            self.app.gcode.probe.xmin = original_xmin
            self.app.gcode.probe.ymin = original_ymin
            messagebox.showerror(_("Error"), f"Failed to run bitsetter calibration probe: {e}")

    def _poll_cal_bltouch_z(self):
        """Poll for bitsetter calibration probe completion, then update Calibrated BLTouch Pos."""
        if self.app.running or self.app.gcode.probe.is_multi_point_scan:
            if self._cal_blt_poll_count < 1200:  # 2-minute timeout
                self._cal_blt_poll_count += 1
                self.app.after(100, self._poll_cal_bltouch_z)
                return
            else:
                messagebox.showerror(_("Error"), _("Timeout waiting for calibration probe after 2 minutes"))
                self.app.gcode.probe.is_multi_point_scan = False
                return

        state = CNC.vars.get("state", "Idle")
        if state == "Alarm":
            messagebox.showerror(_("Error"), _("Probing failed (Alarm state active)."))
            return

        results = self.app.gcode.probe.multi_probe_points
        if results and len(results) > 0:
            prb_z = results[-1][2]  # WCS Z at contact
            # Calibrated BLTouch Pos = probed Z + abs(probe-to-tip offset)
            cal_z = prb_z + self._cal_blt_tip_offset
            self._cal_blt_z_entry.set(f"{cal_z:.4f}")
            messagebox.showinfo(
                _("Calibration Complete"),
                _(f"Probed Z: {prb_z:.4f}\n"
                  f"Tip Offset (abs): {self._cal_blt_tip_offset:.4f}\n"
                  f"Calibrated BLTouch Pos set to: {cal_z:.4f}")
            )
        else:
            messagebox.showerror(_("Error"), _("No probe points recorded during calibration."))

    def run_measure_z_bitsetter(self, dialog, homing_x_entry, homing_z_entry, probe_depth_entry, bltouch_z_entry, feed_rate_entry, home_first_var):
        try:
            bs_x = float(homing_x_entry.get().strip())
            bs_z = float(homing_z_entry.get().strip())
            probe_depth = float(probe_depth_entry.get())
            self._bitsetter_bltouch_z = float(bltouch_z_entry.get())
            feed_rate = float(feed_rate_entry.get())
            home_first = home_first_var.get()
        except ValueError:
            messagebox.showerror(_("Error"), _("Invalid numeric values"))
            return

        # Ensure the probe is retracted before starting homing/movements
        self._retract_probe("[MEASURE_Z_BITSETTER]")

        Utils.setStr("SurfAlign", "bitsetter_x", bs_x)
        Utils.setStr("SurfAlign", "bitsetter_z", bs_z)
        Utils.setFloat("SurfAlign", "bitsetter_probe_depth", probe_depth)
        Utils.setFloat("SurfAlign", "bitsetter_bltouch_z", self._bitsetter_bltouch_z)
        Utils.setFloat("SurfAlign", "bitsetter_feed_rate", feed_rate)
        Utils.setInt("SurfAlign", "bitsetter_home_first", home_first)
        dialog.destroy()

        try:
            start_wy = CNC.vars["wy"]
        except KeyError:
            messagebox.showerror(_("Error"), _("Machine position not available."))
            return

        print(f"Measure Z Bitsetter: X={bs_x}, Z={bs_z}")


        # Probe descends by probe_depth below the start position
        wcs_z_min = bs_z - abs(probe_depth)

        original_xmin = self.app.gcode.probe.xmin
        original_ymin = self.app.gcode.probe.ymin

        try:
            # Prepare jog commands to move to the bitsetter location
            jog_lines = []
            
            if home_first:
                jog_lines.append("$H") # Home the machine
            else:
                jog_lines.append("G53 G0 Z0") # Safety lift to Machine Z=0 (usually top) if not homing
            
            target_wy = start_wy
            jog_lines.append(f"G53 G0 X{bs_x:.4f}") # Move to X
            target_wx = bs_x
            jog_lines.append(f"G53 G0 Z{bs_z:.4f}") # Move to Z safe height
            probe_points = [[target_wx, target_wy]]
            
            # Temporarily set probe boundaries to include target
            self.app.gcode.probe.xmin = min(original_xmin, target_wx)
            self.app.gcode.probe.ymin = min(original_ymin, target_wy)
            
            original_prbfeed = CNC.vars.get("prbfeed", 50.0)
            CNC.vars["prbfeed"] = feed_rate # Set the feed rate for the probe temporarily
            
            scan_lines = self.app.gcode.probe.multi_point_scan(
                probe_points, 
                wcs_z_min, # end_z
                bs_z, # start_z
                0.0, # x_off 
                0.0, # y_off
                0.0  # z_off_field
            )
            
            CNC.vars["prbfeed"] = original_prbfeed # Restore the feed rate for the probe
            
            # Combine jog and scan lines
            full_program = jog_lines + scan_lines
                
            self.app.run(full_program)
            
            self.app.gcode.probe.xmin = original_xmin
            self.app.gcode.probe.ymin = original_ymin

            self._measure_poll_count = 0
            self.app.after(1000, self._poll_measure_z_bitsetter)

        except Exception as e:
            self.app.gcode.probe.xmin = original_xmin
            self.app.gcode.probe.ymin = original_ymin
            messagebox.showerror(_("Error"), f"Failed to generate probe command: {e}")

    def _poll_measure_z_bitsetter(self):
        if self.app.running or self.app.gcode.probe.is_multi_point_scan:
             if self._measure_poll_count < 1200:
                 self._measure_poll_count += 1
                 self.app.after(100, self._poll_measure_z_bitsetter)
                 return
             else:
                 messagebox.showerror(_("Error"), _("Timeout waiting for probe after 2 minutes"))
                 self.app.gcode.probe.is_multi_point_scan = False
                 return

        state = CNC.vars.get("state", "Idle")
        if state == "Alarm":
             messagebox.showerror(_("Error"), _("Probing failed (Alarm state active)."))
             return

        results = self.app.gcode.probe.multi_probe_points
        if results and len(results) > 0:
             last_pt = results[-1]
             prb_wcs_z = last_pt[2]
             

             z_offset = self._bitsetter_bltouch_z - prb_wcs_z
             
             self.z_probe_to_tool_offset.set(f"{z_offset:.4f}")
             messagebox.showinfo(_("Measure Z (Bitsetter)"), _(f"Captured Offset: {z_offset:.4f}\n(Updated Field)"))
        else:
             print("Measure Z (Bitsetter): No probe points recorded.")

    def _retract_probe(self, label="[RETRACT]"):
        """Retract physical probe if deployed; safe to call repeatedly."""
        try:
            if getattr(self, "probe_deployed", False):
                self.app.blt_serial_send('2')
                self.probe_deployed = False
                print(f"{label} probe retract sent")
                return True
        except Exception as e:
            print(f"{label} probe retract error:", e)
        return False

    def generate_probe(self, show_plot=True):
        no_of_points = int(self.n_probe_points.get())
        polynomial_degree = int(self.polynomial_degree.get())
        is_valid, message, _unused = self.validate_probe_points_vs_degree(no_of_points, polynomial_degree)
        if not is_valid:
            messagebox.showerror(_("Probe Configuration Error"), message)
            return False
        try:
            bilin_grid_cell_size_mm = float(self.bilin_grid_cell_size_mm.get())
        except:
            bilin_grid_cell_size_mm = None
        self.probe_points = self.app.gcode.generate_and_plot_probing_points(
            method=self.probe_coverage_method.get(), k=no_of_points, show_plot=show_plot,
            grid_cell_size=bilin_grid_cell_size_mm,
            step_size=float(self.step_size.get() or 1.0)
        )

        print("self.probe_points", self.probe_points)
        return self._has_points()  # <-- instead of bool(self.probe_points)

    def show_probe_points(self):
        if not self._has_points():  # <-- avoid "if not self.probe_points"
            messagebox.showwarning(_("Probe error"), _("No probe points found"))
            return
        popup = tkExtra.ExLabelFrame(self, text=_("Probe Points"), foreground="DarkBlue")
        popup.pack(side=TOP, fill=X)
        for point in self._iter_points():
            try:
                txt = f"Point: {tuple(point)}"
            except Exception:
                txt = f"Point: {point}"
            Label(popup.frame, text=txt).pack(anchor=W)
        Button(popup.frame, text=_("Close"), command=popup.destroy).pack(side=BOTTOM)

    def start_probing(self):
        if not self._has_points():  # <-- avoid "if not self.probe_points"
            messagebox.showwarning(_("Probe error"), _("No probe points found"))
            return False

        x_off = float(self.x_probe_to_tool_offset.get() or 0)
        y_off = float(self.y_probe_to_tool_offset.get() or 0)
        z_off = float(self.z_probe_to_tool_offset.get() or 0)

        print("Start Probing")
        mp_z_min = float(self.mp_z_min.get())
        mp_z_max = float(self.mp_z_max.get())

        # Move Z-up and deploy probe without blocking UI
        try:
            safe_z = mp_z_max + z_off
            if hasattr(self.app, "mcontrol") and hasattr(self.app.mcontrol, "jog"):
                self.app.mcontrol.jog(f"Z{safe_z:.4f}")
        except Exception as e:
            print("Jog Z before deploy error:", e)

        # Delay deploy and then run probing, Tk-friendly
        def _deploy_and_run():
            if self.check_quick_align_stop():  # honor stop before deploying
                return
            try:
                self.app.blt_serial_send('1')  # deploy probe
                self.probe_deployed = True
            except Exception as e:
                print("Deploy probe error:", e)

            # Build probing program and run
            try:
                lines = self.app.gcode.probe.multi_point_scan(
                    self.probe_points, mp_z_min, mp_z_max, x_off, y_off, z_off)
                # Early stop check
                if self.check_quick_align_stop():
                    return
                self.app.run(lines)  # sender should react to feedhold/stop
                print("PROBE COMMAND:\n", "\n".join(lines))
            except Exception as e:
                print("Probing run error:", e)

            return True

        self._deploy_delay_id = self.app.after(500, _deploy_and_run)
        return True

    def quick_align_run(self):

        gen = getattr(self.app, "surfalign_gen_gcode_frame", None)
        if gen is None:
            print("GenGcodeFrame not found; skipping G-code generation")
            return
        try:
            generated = gen.generateGcode()
        except Exception as e:
            print("GenerateGcode failed:", e)
            generated = False
        if not generated:
            # Probing and running now would cut whatever G-code was loaded before.
            self.app.setStatus(_("Align & Run cancelled - G-code generation failed."))
            return

        # Start fresh: clear any previous stop & cancel previous timers
        self._cancel_after_callbacks()
        self.stop_quick_align = False
        self.quick_align_active = True

        no_of_points = int(self.n_probe_points.get())
        polynomial_degree = int(self.polynomial_degree.get())
        # Not `_`: that would make the translation function local to this method
        is_valid, message, _unused = self.validate_probe_points_vs_degree(no_of_points, polynomial_degree)
        if not is_valid:
            self.quick_align_active = False
            messagebox.showerror(_("Probe Configuration Error"), message)
            return False

        # Step 1: generate points (no plot)
        success = self.generate_probe(show_plot=False)
        print("PROBE POINTS GENERATED", success)
        if not success or self.check_quick_align_stop():
            self.quick_align_active = False
            return

        # Step 2: start probing
        try:
            self.app.gcode.probe.start_multi_point_scan = True
        except Exception:
            pass

        success = self.start_probing()
        print("PROBING STARTED", success)
        if not success or self.check_quick_align_stop():
            self.quick_align_active = False
            return

        # Step 3: poll probing status
        self._poll_id = self.app.after(1000, self._poll_probe_status)

    def _poll_probe_status(self):
        if self.check_quick_align_stop():
            return
        try:
            if self.app.gcode.probe.start_multi_point_scan:
                self._poll_id = self.app.after(300, self._poll_probe_status)
                return
        except Exception:
            pass
        print("PROBING COMPLETED")

        # Retract on normal completion
        self._retract_probe("[COMPLETE]")

        self._process_id = self.app.after(5000, self._process_alignment_results)

    def _process_alignment_results(self):
        if self.check_quick_align_stop():
            return

        self.app.gcode.x_probe_to_tool_offset = float(self.x_probe_to_tool_offset.get() or 0)
        self.app.gcode.y_probe_to_tool_offset = float(self.y_probe_to_tool_offset.get() or 0)
        self.app.gcode.z_probe_to_tool_offset = float(self.z_probe_to_tool_offset.get() or 0)

        polynomial_degree = int(self.polynomial_degree.get())
        bounds = self.app.gcode.surf_align_gcode(self.app.editor.getAllBlocks(),
                                                 step_size=float(self.step_size.get()),
                                                 degree=polynomial_degree,
                                                 method=self.probe_coverage_method.get())
        self.app.drawAfter()
        print("Bounds:", bounds)
        print("SURF ALIGN GCODE COMPLETED")

        if bounds is None:
            self.quick_align_active = False
            messagebox.showwarning(_("Probing Error 0"), _("No probe points found 0"))
            return
        z_min = bounds.get("z_min")
        if z_min is None:
            self.quick_align_active = False
            messagebox.showwarning(_("Probing Error 1"), _("No probe points found 1"))
            return

        if z_min < float(self.z_safety_limit.get()):
            self.quick_align_active = False
            self.app.event_generate("<<Undo>>")
            messagebox.showwarning(_("Safety Limit Error"),
                                   _("Z-min is below the safety limit. Please adjust the Z-min safety limit."))
            return

        self._run_id = self.app.after(1000, self._gcode_run_command)

    def _gcode_run_command(self):
        if self.check_quick_align_stop():
            print("STOPPED QUICK ALIGN prevented RUN")
            return
        # From here the run itself keeps the machine busy (app.running)
        self.quick_align_active = False
        try:
            self.app.run()
            print("GCODE RUN COMMAND SENT")
        except Exception as e:
            print("Run error:", e)

    def quick_align_stop(self):
        """Handler for the red 'Stop Running' button."""
        # Sticky stop; don't clear it here
        self.safe_stop_all(use_hard_stop_fallback=True, timeout_ms=1500)

    def safe_stop_all(self, use_hard_stop_fallback=True, timeout_ms=1500):
        """
        Gentle stop first:
          1) Latch stop flag, stop your pipeline
          2) <<Pause>> (FEED_HOLD) so planner decelerates
          3) Stop streaming queued lines (if API available)
          4) Wait briefly for Hold/Idle
          5) Try to stop spindle/coolant (best effort)
          6) Cancel any scheduled callbacks
        If not Hold/Idle in time and fallback is allowed -> <<Stop>> (soft reset).
        """
        # 1) Latch stop and mark probing not running
        self.stop_quick_align = True
        self.quick_align_active = False
        try:
            self.app.gcode.probe.start_multi_point_scan = False
        except Exception:
            pass

        # 2) Ask bCNC to FEED_HOLD using your default mapping
        try:
            self.app.event_generate("<<Pause>>")  # FEED_HOLD (!)
            print("[STOP] <<Pause>> (feed hold) sent")
        except Exception as e:
            print("[STOP] Could not send <<Pause>>:", e)

        # 3) Stop streaming queued gcode (if your app exposes it)
        try:
            if hasattr(self.app, "stop"):
                self.app.stop()  # many bCNC forks expose this
                print("[STOP] sender.stop() called")
        except Exception as e:
            print("[STOP] sender.stop() error:", e)

        # 4) Wait briefly for Hold/Idle; if it never arrives, we may hard-stop
        if not self._wait_for_hold_or_idle(timeout_ms=timeout_ms, poll_ms=50):
            print("[STOP] Not in Hold/Idle within timeout")

        # 5) Best-effort: stop spindle & coolant (safe even if already stopped)
        try:
            self.app.run(["M5", "M9"])  # won’t succeed on every controller while held; harmless if ignored
            print("[STOP] M5/M9 sent")
        except Exception as e:
            print("[STOP] M5/M9 error:", e)

        # 5.1) Retract probe now (gentle path)
        self._retract_probe("[STOP]")

        # 6) Cancel callbacks
        self._cancel_after_callbacks()

        # 7) If we never got to a stable state and fallback allowed -> soft reset
        if use_hard_stop_fallback:
            st = getattr(getattr(self.app, "status", None), "state", None)
            if st not in ("Idle", "Hold"):
                try:
                    self.app.event_generate("<<Stop>>")  # your default: soft reset / flush
                    print("[STOP] <<Stop>> (soft reset) sent")
                except Exception as e:
                    print("[STOP] Could not send <<Stop>>:", e)
                # Controllers can ignore immediate commands after reset—retry retract shortly
                try:
                    if self._retract_id:
                        self.app.after_cancel(self._retract_id)
                    self._retract_id = self.app.after(300, lambda: self._retract_probe("[STOP-FB]"))
                except Exception:
                    pass

        print("[STOP] Completed stop pipeline (sticky stop latched)")

    def _cancel_after_callbacks(self):
        for attr in ("_poll_id", "_process_id", "_run_id", "_deploy_delay_id", "_retract_id"):
            aid = getattr(self, attr, None)
            if aid:
                try:
                    self.app.after_cancel(aid)
                except Exception:
                    pass
                setattr(self, attr, None)

    def _wait_for_hold_or_idle(self, timeout_ms=1500, poll_ms=50):
        """Poll app.status.state until 'Hold' or 'Idle' or timeout. Returns bool."""
        waited = 0
        while waited < timeout_ms:
            try:
                st = getattr(getattr(self.app, "status", None), "state", None)
                if st in ("Hold", "Idle"):
                    return True
            except Exception:
                pass
            # Keep UI responsive; micro-yield to Tk
            self._deploy_delay_id = self.app.after(poll_ms, lambda: None)
            try:
                self.app.update_idletasks()
            except Exception:
                pass
            try:
                self.app.after_cancel(self._deploy_delay_id)
            except Exception:
                pass
            self._deploy_delay_id = None
            waited += poll_ms
        return False

    def check_quick_align_stop(self):
        """
        Respect sticky stop. IMPORTANT: do NOT clear the flag here.
        Also force probing loop to end.
        """
        if self.stop_quick_align:
            self.quick_align_active = False
            try:
                self.app.gcode.probe.start_multi_point_scan = False
            except Exception:
                pass
            print("[STOP] Sticky stop is set")
            return True
        return False


# =============================================================================
# Lid Engravings page layout
# =============================================================================
class _Section(Frame):
    """A titled block that folds away behind a full-width header button."""

    def __init__(self, master, title, expanded):
        Frame.__init__(self, master)
        self.title = title
        self.header = Button(self, anchor=W, relief="flat", font=("", 11, "bold"),
                             bg="#dde6ee", activebackground="#c9d6e2", padx=6, pady=3,
                             command=self.toggle)
        self.header.pack(side=TOP, fill=X)
        self.body = Frame(self)
        self.expanded = None
        self.set_expanded(expanded)

    def set_expanded(self, expanded):
        if expanded == self.expanded:
            return
        self.expanded = expanded
        self.header.config(text=("▼  " if expanded else "▶  ") + self.title)
        if expanded:
            self.body.pack(side=TOP, fill=X, padx=(4, 0), pady=(0, 2))
        else:
            self.body.pack_forget()

    def toggle(self):
        self.set_expanded(not self.expanded)


class LidEngravingsFrame(CNCRibbon.PageFrame):
    """The whole Lid Engravings page, in one vertically scrolling column:

        Tote Scanning      (open)   - engraver login, scan, the tote's lids
        Advanced Settings  (closed) - probe, G-code and surface-probe settings
        preview controls            - text, lid, font size, vertical centering
        Align & Run / Stop

    SurfAlignPage.register builds the frames that fill it.
    """

    def __init__(self, master, app):
        CNCRibbon.PageFrame.__init__(self, master, "LidEngravings", app)
        self.app.lid_engravings = self
        self.multipoint = None      # MultiPointProbe, set once it is built

        # width/height=1: the pane's size decides, not the canvas' default request
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, width=1, height=1)
        self.scrollbar = Scrollbar(self, orient=VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.canvas.pack(side=LEFT, fill=BOTH, expand=YES)
        body = self.body = Frame(self.canvas)
        self._window = self.canvas.create_window(0, 0, window=body, anchor=NW)
        body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self.bind_all("<MouseWheel>", self._on_wheel, add="+")

        self.scanning = _Section(body, _("Tote Scanning"), expanded=True)
        self.scanning.pack(side=TOP, fill=X, pady=(2, 0))
        self.advanced = _Section(body, _("Advanced Settings"), expanded=False)
        self.advanced.pack(side=TOP, fill=X, pady=(2, 0))
        self.setup_bar = Frame(self.advanced.body)
        self.setup_bar.pack(side=TOP, fill=X, pady=(2, 4))

        self.common = LabelFrame(body, text=_("Check the Preview"), foreground="DarkBlue",
                                 padx=6, pady=4)
        self.common.pack(side=TOP, fill=X, pady=(4, 0))
        self.run_bar = Frame(body)
        self.run_bar.pack(side=TOP, fill=X, pady=(6, 4))

    def add_setup_buttons(self, engraving, gen):
        for text, command in ((_("Engraving Station…"), engraving.show_settings_dialog),
                              (_("ShipHero & Assets…"), gen.show_shiphero_and_asset_config_dialog)):
            Button(self.setup_bar, text=text, command=command).pack(
                side=LEFT, fill=X, expand=YES, padx=2)

    # ------------------------------------------------------------------ state
    def busy(self):
        """Machine running, or Align & Run still probing/aligning before its run."""
        return bool(self.app.running or (self.multipoint is not None
                                          and self.multipoint.quick_align_active))

    # --------------------------------------------------------------- showing
    def show_scanning(self):
        """Bring the page forward with Tote Scanning open and in view."""
        self.app.ribbon.changePage("SurfAlign")
        self.scanning.set_expanded(True)
        self.see(self.scanning)

    def show_run(self):
        self.see(self.run_bar)

    def see(self, widget):
        """Scroll the least needed to bring `widget` fully into view."""
        self.update_idletasks()
        body_h = self.body.winfo_height()
        view_h = self.canvas.winfo_height()
        if body_h <= view_h or body_h <= 1:
            return
        top = widget.winfo_rooty() - self.body.winfo_rooty()
        bottom = top + widget.winfo_height()
        first, last = self.canvas.yview()
        if top < first * body_h or widget.winfo_height() > view_h:
            self.canvas.yview_moveto(top / body_h)
        elif bottom > last * body_h:
            self.canvas.yview_moveto((bottom - view_h) / body_h)

    # ------------------------------------------------------------- scrolling
    def _on_yscroll(self, first, last):
        self.scrollbar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.scrollbar.pack_forget()
        elif not self.scrollbar.winfo_ismapped():
            self.scrollbar.pack(side=RIGHT, fill=Y, before=self.canvas)

    def _on_wheel(self, event):
        # bind_all sees every wheel event in the app: act only on this page's
        # own widgets, and leave the lid list to scroll itself.
        path = str(event.widget)
        canvas = str(self.canvas)
        if not (path == canvas or path.startswith(canvas + ".")):
            return
        if isinstance(event.widget, ttk.Treeview):
            return
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")


# =============================================================================
# Probe Page
# =============================================================================
class SurfAlignPage(CNCRibbon.Page):
    __doc__ = _("Scan totes, check the preview, align and engrave lids")
    _name_ = "SurfAlign"
    _label_ = "Lid Engravings"
    _icon_ = "measure"

    # -----------------------------------------------------------------------
    # Add a widget in the widgets list to enable disable during the run
    # -----------------------------------------------------------------------
    def register(self):
        from EngravingPage import EngravingFrame

        self._register((ProbeTabGroup,), None)

        # One scrolling page; the frames are built straight into its sections.
        page = LidEngravingsFrame(self.master._pageFrame, self.app)
        CNCRibbon.Page.frames[page.name] = page
        advanced = page.advanced.body
        engraving = EngravingFrame(page.scanning.body, self.app)
        gen = GenGcodeFrame(advanced, self.app, common=page.common)
        page.multipoint = MultiPointProbe(advanced, self.app, run_bar=page.run_bar)
        for frame in (engraving, ProbeCommonFrame(advanced, self.app), gen, page.multipoint):
            CNCRibbon.Page.frames[frame.name] = frame
            frame.pack(side=TOP, fill=X)
        page.add_setup_buttons(engraving, gen)

        self.tabGroup = CNCRibbon.Page.groups["Probe"]
        self.tabGroup.tab.set("Probe")
        self.tabGroup.tab.trace("w", self.tabChange)

    # -----------------------------------------------------------------------
    def tabChange(self, a=None, b=None, c=None):
        tab = self.tabGroup.tab.get()
        self.master._forgetPage()

        # Remove all page tabs with ":" and add the new ones
        self.ribbons = [x for x in self.ribbons if ":" not in x[0].name]
        self.frames = [x for x in self.frames if ":" not in x[0].name]

        try:
            self.addRibbonGroup(f"Probe:{tab}")
        except KeyError:
            pass
        try:
            self.addPageFrame(f"Probe:{tab}")
        except KeyError:
            pass

        self.master.changePage(self)
