# $Id$
#
# Author: Vasilis Vlachoudis
#  Email: vvlachoudis@gmail.com
#   Date: 18-Jun-2015

# import time
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
    VERTICAL,
    Scrollbar,
)

import Camera
import CNCRibbon
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

    def __init__(self, master, app):
        CNCRibbon.PageFrame.__init__(self, master, "GenGcode", app)

        lframe = tkExtra.ExLabelFrame(
            self, text=_("GCode"), foreground="DarkBlue")
        lframe.pack(side=TOP, fill=X)
        frame = lframe.frame

        # ----
        row = 0
        col = 0

        # ----
        # Fast Probe Feed
        Label(frame,
              text=_("Engrave Text:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.engraveText = StringVar()
        GenGcodeFrame.engraveText = Entry(
            frame,
            background=tkExtra.GLOBAL_CONTROL_BACKGROUND,
            width=5,
            textvariable=self.engraveText,
        )
        GenGcodeFrame.engraveText.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            GenGcodeFrame.engraveText,
            _("Text to engrave. Use | to separate words with spacing. Use <|> for literal pipe character."),
        )
        self.addWidget(GenGcodeFrame.engraveText)

        # Add info button for text syntax help
        col += 1
        info_button = Button(frame, text="ℹ", font=("TkDefaultFont", 8, "bold"), 
                            command=self.show_text_syntax_help, width=2, height=1)
        info_button.grid(row=row, column=col, sticky=W, padx=(2, 0))
        tkExtra.Balloon.set(info_button, _("Click for detailed text syntax help"))
        self.addWidget(info_button)

        # Font selection dropdown
        row += 1
        col = 0
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

        # ----
        # Font Size 
        row += 1
        col = 0
        Label(lframe(), text=_("Font Size:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.fontSize = tkExtra.FloatEntry(
            lframe(), background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.fontSize.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.fontSize, _("Engrave Text Size"))
        
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
        self.lidName = StringVar()
        self.lidName_selector = ttk.Combobox(lframe(), textvariable=self.lidName, values=self.lid_list, width=30, state="readonly")
        self.lidName_selector.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(self.lidName_selector, _("Select lid name"))
        self.addWidget(self.lidName_selector)
        
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
        
    def loadConfig(self):
        self.engraveText.set(Utils.getStr("SurfAlign", "engraveText"))
        self.font_var.set(Utils.getStr("SurfAlign", "textFont"))
        self.fontSize.set(Utils.getFloat("SurfAlign", "fontSize"))
        self.posX.set(Utils.getFloat("SurfAlign", "posX"))
        self.posY.set(Utils.getFloat("SurfAlign", "posY"))
        self.textPositioning_var.set(Utils.getStr("SurfAlign", "textPositioningMode"))
        self.lidName.set(Utils.getStr("SurfAlign", "lidName"))
        self.center_offset_x.set(Utils.getFloat("SurfAlign", "centerOffsetX"))
        self.center_offset_y.set(Utils.getFloat("SurfAlign", "centerOffsetY"))
        self.rotation.set(Utils.getFloat("SurfAlign", "rotation"))
        self.feedrate.set(Utils.getFloat("SurfAlign", "feedrate"))
        self.spindleRPM.set(Utils.getFloat("SurfAlign", "spindleRPM"))
        self.engraveDepth.set(Utils.getFloat("SurfAlign", "engraveDepth"))
        self.layerHeight.set(Utils.getFloat("SurfAlign", "layerHeight"))
        self.safeHeight.set(Utils.getFloat("SurfAlign", "safeHeight"))
        self.finalHeight.set(Utils.getFloat("SurfAlign", "finalHeight"))
        self.gapDistance.set(Utils.getFloat("SurfAlign", "gapDistance"))
        
        self.on_text_positioning_change(None)
        
    def generateGcode(self):
        print("Generate Gcode")
        engrave_text = self.engraveText.get()
        work_area_width, work_area_height = 500, 500
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
                                                   gap_distance_mm)
            
            print("Generated Gcode file path:", gcode_file_path)
            
            # Check if the file was actually created
            if not os.path.exists(gcode_file_path):
                messagebox.showerror(_("GCode Generation Error"), 
                                   _("GCode file was not created successfully. Please check the parameters and try again."))
                return
                
            self.app.load(gcode_file_path)
            print("Loaded Gcode file:", self.app.gcode.filename)
            
        except Exception as e:
            messagebox.showerror(_("GCode Generation Error"), 
                               _("GCode generation failed. Please check the parameters and try again."))
            print(f"GCode generation error: {e}")
            import traceback
            traceback.print_exc()




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

        

    def show_edit_lid_dialog(self):
        """Show a popup dialog for adding/deleting lid names."""
        dialog = Toplevel(self)
        dialog.title(_("Add/Delete Lid"))
        dialog.geometry("400x500")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("+%d+%d" % (self.winfo_rootx() + 50, self.winfo_rooty() + 50))
        
        # Add new lid section
        add_frame = LabelFrame(dialog, text=_("Add New Lid"), padx=10, pady=10)
        add_frame.pack(fill=X, padx=10, pady=(10, 5))
        
        Label(add_frame, text=_("Lid Name:")).grid(row=0, column=0, sticky=W, pady=(0, 5))
        new_lid_entry = Entry(add_frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND, width=25)
        new_lid_entry.grid(row=1, column=0, columnspan=2, sticky=EW, pady=(0, 5))
        new_lid_entry.focus_set()
        
        Label(add_frame, text=_("Format: {name}-{height}x{width} (e.g., Vitamin_XL_Pill-300x1200)"), 
              fg="gray", font=("TkDefaultFont", 8)).grid(row=2, column=0, columnspan=2, sticky=W, pady=(0, 5))
        
        Label(add_frame, text=_("Note: All dimensions are in millimeters (mm)"), 
              fg="blue", font=("TkDefaultFont", 8)).grid(row=3, column=0, columnspan=2, sticky=W, pady=(0, 5))
        
        add_button = Button(add_frame, text=_("Add"), command=lambda: self.add_lid_from_dialog(new_lid_entry.get().strip(), dialog, lid_listbox), padx=10, pady=2)
        add_button.grid(row=4, column=0, sticky=W, pady=(5, 0))
        
        # Existing lids section
        existing_frame = LabelFrame(dialog, text=_("Existing Lids"), padx=10, pady=10)
        existing_frame.pack(fill=BOTH, expand=True, padx=10, pady=(5, 10))
        
        # Listbox with scrollbar for existing lids
        list_frame = Frame(existing_frame)
        list_frame.pack(fill=BOTH, expand=True)
        
        lid_listbox = tkinter.Listbox(list_frame, height=8)
        lid_listbox.pack(side=LEFT, fill=BOTH, expand=True)
        
        scrollbar = tkinter.Scrollbar(list_frame, orient=VERTICAL, command=lid_listbox.yview)
        scrollbar.pack(side=RIGHT, fill=Y)
        lid_listbox.config(yscrollcommand=scrollbar.set)
        
        # Populate listbox with existing lids
        for lid in self.lid_list:
            lid_listbox.insert(END, lid)
        
        # Delete button
        delete_button = Button(existing_frame, text=_("Delete Selected"), 
                              command=lambda: self.delete_lid_from_dialog(lid_listbox, dialog), 
                              padx=10, pady=2, bg="#F44336", fg="white")
        delete_button.pack(pady=(5, 0))
        
        # Bottom buttons
        button_frame = Frame(dialog)
        button_frame.pack(fill=X, padx=10, pady=(0, 10))
        
        Button(button_frame, text=_("Close"), command=dialog.destroy, padx=10, pady=2).pack(side=RIGHT)
        
        # Bind events
        new_lid_entry.bind('<Return>', lambda event: self.add_lid_from_dialog(new_lid_entry.get().strip(), dialog, lid_listbox))
        dialog.bind('<Escape>', lambda event: dialog.destroy())
        
        add_frame.columnconfigure(1, weight=1)

    def validate_lid_format(self, lid_name):
        """Validate that the lid name follows the format {name}-{height}x{width}."""
        if not lid_name or '-' not in lid_name or 'x' not in lid_name:
            return _("Lid name must follow format: {name}-{height}x{width}")
        
        parts = lid_name.split('-')
        if len(parts) != 2 or not parts[0] or not re.match(r'^[a-zA-Z0-9_]+$', parts[0]):
            return _("Name part must contain only letters, numbers, and underscores")
        
        dimensions = parts[1].split('x')
        if len(dimensions) != 2:
            return _("Dimensions must be in format: {height}x{width}")
        
        try:
            height, width = float(dimensions[0]), float(dimensions[1])
            if height <= 0 or width <= 0:
                return _("Height and width must be positive numbers")
        except ValueError:
            return _("Height and width must be valid numbers")
        
        return None  # Valid

    def extract_lid_dimensions(self, lid_name):
        """Extract height and width from a valid lid name."""
        if self.validate_lid_format(lid_name):
            return None, None
        try:
            parts = lid_name.split('-')[1].split('x')
            return float(parts[0]), float(parts[1])
        except (IndexError, ValueError):
            return None, None

    def add_lid_from_dialog(self, new_lid_name, dialog, lid_listbox):
        """Add a new lid name from the dialog and close it."""
        if not new_lid_name:
            messagebox.showwarning(_("Empty Entry"), _("Please enter a lid name."), parent=dialog)
            return
        
        error_message = self.validate_lid_format(new_lid_name)
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
        """Delete a selected lid name from the list."""
        selected_index = lid_listbox.curselection()
        if not selected_index:
            messagebox.showwarning(_("No Selection"), _("Please select a lid to delete."), parent=dialog)
            return
            
        selected_lid = lid_listbox.get(selected_index[0])
        
        # Confirm deletion
        if not messagebox.askyesno(_("Confirm Delete"), 
                                  _("Are you sure you want to delete '{}'?").format(selected_lid), 
                                  parent=dialog):
            return
            
        self.lid_list.remove(selected_lid)
        self.lidName_selector['values'] = self.lid_list
        
        # Clear the current selection if it was the deleted one
        if self.lidName.get() == selected_lid:
            self.lidName.set("")
        
        # Refresh the listbox
        lid_listbox.delete(0, END)
        for lid in self.lid_list:
            lid_listbox.insert(END, lid)
        
        self.saveConfig()
        messagebox.showinfo(_("Success"), _("Lid name '{}' has been deleted from the list.").format(selected_lid), parent=dialog)

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
        """Extract width and height from the currently selected lid name in the selector.
        
        Returns:
            tuple: (width, height) in mm, or (None, None) if no lid selected or invalid format
        """
        lid_name = self.lidName.get()
        if not lid_name:
            return None, None
            
        # Validate the format first
        error_message = self.validate_lid_format(lid_name)
        if error_message:
            return None, None
            
        try:
            # Split by '-' to get the dimensions part
            parts = lid_name.split('-')
            if len(parts) != 2:
                return None, None
                
            # Split the dimensions part by 'x' to get width and height
            dimensions = parts[1].split('x')
            if len(dimensions) != 2:
                return None, None
                
            # Convert to floats - format is "name-widthxheight"
            width = float(dimensions[0])
            height = float(dimensions[1])
            
            return width, height
            
        except (ValueError, IndexError):
            return None, None

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
    def __init__(self, master, app):
        """Initialize the MultiPointProbe class for polynomial surface probing."""
        CNCRibbon.PageFrame.__init__(self, master, "MultiPointProbe", app)
        
        
        self.probe_points = []  # Dummy list of (X, Y) coordinates
        self.stop_quick_align = False
        
        # UI Elements
        lframe = tkExtra.ExLabelFrame(self, text=_("Multi-Point Surface Probe"), foreground="DarkBlue")
        lframe.pack(side=TOP, fill=X)
        frame = lframe.frame
        
        
        
        # --- Input Fields ---
        row, col = 0, 0
        Label(frame, text=_("No. of Points:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.n_probe_points = tkExtra.IntegerEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.n_probe_points.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.n_probe_points, _("Number of probe points"))
        
        row += 1
        col = 0
        Label(frame, text=_("Z Min, Max:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.mp_z_min = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.mp_z_min.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.mp_z_min, _("Z minimum depth to scan"))
        
        col += 1
        self.mp_z_max = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.mp_z_max.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.mp_z_max, _("Z safe to move"))


        
        # feed command
        row += 1
        col = 0
        Label(frame,
              text=_("Probe Coverage Method")).grid(row=row, column=col, sticky=E)
        col += 1
        self.probe_coverage_methods = ["EvenCoverage", "AreaCoverage"]
        self.probe_coverage_method = tkExtra.Combobox(
            frame,
            True,
            background=tkExtra.GLOBAL_CONTROL_BACKGROUND,
            width=16,
        )
        self.probe_coverage_method.grid(row=row, column=col, sticky=EW)
        self.probe_coverage_method.fill(self.probe_coverage_methods)
        self.probe_coverage_method.set("EvenCoverage")
        self.addWidget(self.probe_coverage_method)
        
        
        
        # --- ALL Axis Offset (Probe → Tool) ---
        row+=1
        col = 0
        Label(frame, text=_("Offset (Probe → Tool):")).grid(row=row, column=col, sticky=E)
        row+=1
        col = 0
        self.x_probe_to_tool_offset = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.x_probe_to_tool_offset.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.x_probe_to_tool_offset, _("X offset (mm) between probe and tool.")
        )
        self.addWidget(self.x_probe_to_tool_offset)
        col += 1
        self.y_probe_to_tool_offset = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.y_probe_to_tool_offset.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.y_probe_to_tool_offset, _("Y offset (mm) between probe and tool.")
        )   
        self.addWidget(self.y_probe_to_tool_offset)
        col += 1
        self.z_probe_to_tool_offset = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.z_probe_to_tool_offset.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.z_probe_to_tool_offset, _("Z offset (mm) between probe and tool.")
        )
        self.addWidget(self.z_probe_to_tool_offset)
        
        row += 1
        col = 0
        Label(frame, text=_("Step Size:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.step_size = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.step_size.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.step_size, _("Distance between G-code points when creating surface-aligned toolpaths. Smaller values create smoother curves but more G-code commands. Use larger values when polynomial degree is 1 (flat plane).")
        )
        self.addWidget(self.step_size)
        
        row += 1
        col = 0
        Label(frame, text=_("Poly Degree:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.polynomial_degree = tkExtra.IntegerEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.polynomial_degree.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.polynomial_degree, _("Degree of the polynomial surface to fit the probe points ( 1 = Flat plane).")
        )
        self.addWidget(self.polynomial_degree)
        
        
        # Add validation status label
        row += 1
        col = 0
        self.validation_status = Label(frame, text="", fg="gray", font=("TkDefaultFont", 8))
        self.validation_status.grid(row=row, column=col, columnspan=3, sticky=W)
        self.addWidget(self.validation_status)
        
        # Bind validation updates
        self.n_probe_points.bind('<KeyRelease>', self.update_validation_status)
        self.polynomial_degree.bind('<KeyRelease>', self.update_validation_status)
        
        # --- Generate Probe & Show Probe Points & Start Probing ---

        row += 1
        col = 0
        probe_generate_b = Button(frame, text=_("Generate Probe"), command=self.generate_probe)
        probe_generate_b.grid(row=row, column=col, sticky=W)
        self.addWidget(probe_generate_b)

        col = 1
        # --- Button to Show Probe Points ---
        show_button = Button(frame, text=_("Show Probe Points"), command=self.show_probe_points)
        show_button.grid(row=row, column=col, sticky=W)
        self.addWidget(show_button)



        
        
        col += 1
        probe_generate_b = Button(frame, text=_("Start Probing"), command=self.start_probing)
        probe_generate_b.grid(row=row, column=col, sticky=W)
        self.addWidget(probe_generate_b)
        
        

        
        
        
        row += 1
        col = 0
        select_all_button = Button(
            frame,
            text=_("Select All"),
            command=lambda: self.app.event_generate("<<SelectAll>>")
        )
        select_all_button.grid(row=row, column=col, sticky=W)
        self.addWidget(select_all_button)
        col += 1
        surf_align_gcode_b = Button(frame, text=_("Surface Align G-Code"), command=self.surface_align_gcode)
        surf_align_gcode_b.grid(row=row, column=col, sticky=W)
        self.addWidget(surf_align_gcode_b)
        
        
                # Add new Z safety limit field
        row += 1
        col = 0
        Label(frame, text=_("Z Min Safety Limit:")).grid(row=row, column=col, sticky=E)
        col += 1
        self.z_safety_limit = tkExtra.FloatEntry(
            frame, background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.z_safety_limit.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.z_safety_limit, _("Program will prevent execution if any G-code command Z coordinate goes below this point"))
        self.addWidget(self.z_safety_limit)
        
        # Add Quick Align & Run button with green color
        row += 1
        col = 0
        quick_align_run_b = Button(
            frame, 
            text=_("Quick Align & Run"), 
            command=self.quick_align_run,
            bg="#4CAF50",  # Material Design Green
            fg="white",    # White text
            activebackground="#45a049",  # Slightly darker green when pressed
            activeforeground="white"
        )
        quick_align_run_b.grid(row=row, column=col, sticky=W)
        self.addWidget(quick_align_run_b)
        
        # Add detailed tooltip
        tkExtra.Balloon.set(
            quick_align_run_b, 
            _("Quick Align & Run\n\n"
              "Performs a complete surface alignment process:\n"
              "1. Generate probe points\n"
              "2. Start probing\n"
              "3. Select all points\n"
              "4. Surface align G-code\n"
              "5. Run the G-code\n\n"
              "Use this button to quickly align and run in one step.")
        )
        
                
        col += 1
        quick_align_stop_b = Button(
            frame,
            text=_("Stop Running"), 
            command=self.quick_align_stop,
            bg="#F44336",  # Material Design Red
            fg="white",    # White text
            activebackground="#d32f2f",  # Darker red when pressed
            activeforeground="white"
        )
        quick_align_stop_b.grid(row=row, column=col, sticky=W)
        
        
        

        
        
        
        frame.grid_columnconfigure(1, weight=1)
        self.loadConfig()
        
    def loadConfig(self):
        self.mp_z_min.set(Utils.getFloat("SurfAlign", "mp_z_min"))
        self.mp_z_max.set(Utils.getFloat("SurfAlign", "mp_z_max"))
        self.n_probe_points.set(Utils.getInt("SurfAlign", "n_probe_points"))
        self.x_probe_to_tool_offset.set(Utils.getFloat("SurfAlign", "x_probe_to_tool_offset"))
        self.y_probe_to_tool_offset.set(Utils.getFloat("SurfAlign", "y_probe_to_tool_offset"))
        self.z_probe_to_tool_offset.set(Utils.getFloat("SurfAlign", "z_probe_to_tool_offset"))
        self.z_safety_limit.set(Utils.getFloat("SurfAlign", "z_safety_limit"))
        self.step_size.set(Utils.getFloat("SurfAlign", "step_size"))
        self.polynomial_degree.set(Utils.getInt("SurfAlign", "polynomial_degree"))
        
        # Update validation status after loading config
        self.update_validation_status()
        
    def saveConfig(self):
        Utils.setFloat("SurfAlign", "mp_z_min", self.mp_z_min.get())
        Utils.setFloat("SurfAlign", "mp_z_max", self.mp_z_max.get())
        Utils.setInt("SurfAlign", "n_probe_points", self.n_probe_points.get())
        Utils.setFloat("SurfAlign", "x_probe_to_tool_offset", self.x_probe_to_tool_offset.get())
        Utils.setFloat("SurfAlign", "y_probe_to_tool_offset", self.y_probe_to_tool_offset.get())
        Utils.setFloat("SurfAlign", "z_probe_to_tool_offset", self.z_probe_to_tool_offset.get())
        Utils.setFloat("SurfAlign", "z_safety_limit", self.z_safety_limit.get())
        Utils.setFloat("SurfAlign", "step_size", self.step_size.get())
        Utils.setInt("SurfAlign", "polynomial_degree", self.polynomial_degree.get())
        
    def surface_align_gcode(self):
        if  self.x_probe_to_tool_offset.get() != "":
            self.app.gcode.x_probe_to_tool_offset = float(self.x_probe_to_tool_offset.get())
        else:
            self.app.gcode.x_probe_to_tool_offset = 0
        if self.y_probe_to_tool_offset.get() != "":
            self.app.gcode.y_probe_to_tool_offset = float(self.y_probe_to_tool_offset.get())
        else:
            self.app.gcode.y_probe_to_tool_offset = 0
        if self.z_probe_to_tool_offset.get() != "":
            self.app.gcode.z_probe_to_tool_offset = float(self.z_probe_to_tool_offset.get())
        else:
            self.app.gcode.z_probe_to_tool_offset = 0 
        # self.app.insertCommand("SURF_ALIGN", True)
        
        no_of_points = int(self.n_probe_points.get())
        polynomial_degree = int(self.polynomial_degree.get())
        
        # Enhanced validation
        is_valid, message, recommended_points = self.validate_probe_points_vs_degree(no_of_points, polynomial_degree)
        
        if not is_valid:
            messagebox.showerror(_("Probe Configuration Error"), message)
            return False
        
        bounds = self.app.gcode.surf_align_gcode(self.app.editor.getSelectedBlocks(), step_size=float(self.step_size.get()), degree=polynomial_degree)
        self.app.drawAfter()
    
    def validate_probe_points_vs_degree(self, num_points, degree):
        """
        Validate if the number of probe points is sufficient for the polynomial degree.
        
        Args:
            num_points (int): Number of probe points
            degree (int): Polynomial degree
            
        Returns:
            tuple: (is_valid, message, recommended_points)
        """
        # Calculate minimum points needed (number of coefficients)
        min_points = (degree + 1) * (degree + 2) // 2
        ratio = num_points / min_points
        
        # Basic validation: need at least as many points as coefficients
        if num_points < min_points:
            message = f"❌ Insufficient probe points for polynomial degree {degree}.\n" \
                     f"   Need at least {min_points} points, but only {num_points} provided.\n" \
                     f"   Recommended: {int(min_points * 1.2)} points for reliable fitting."
            return False, message, int(min_points * 1.2)
        
        # Check for good ratio (at least 1.2)
        if ratio >= 1.2:
            message = f"✅ Good: {num_points} points for polynomial degree {degree} ({min_points} coefficients).\n" 
            return True, message, None
        
        # Warning: ratio below 1.2
        else:
            message = f"⚠️  Warning: Low probe points for polynomial degree {degree}.\n"   
            return True, message, int(min_points * 1.2)

    def update_validation_status(self, event=None):
        """Update the validation status display in real-time."""
        try:
            num_points = int(self.n_probe_points.get())
            degree = int(self.polynomial_degree.get())
            
            is_valid, message, recommended_points = self.validate_probe_points_vs_degree(num_points, degree)
            
            # Extract the status part of the message
            if "❌" in message:
                self.validation_status.config(text=message.split('\n')[0], fg="red")
            elif "⚠️" in message:
                self.validation_status.config(text=message.split('\n')[0], fg="orange")
            elif "✅" in message:
                self.validation_status.config(text=message.split('\n')[0], fg="green")
            else:
                self.validation_status.config(text="", fg="gray")
                
        except (ValueError, TypeError):
            self.validation_status.config(text="", fg="gray")

    def generate_probe(self, show_plot=True):
        
        no_of_points = int(self.n_probe_points.get())
        polynomial_degree = int(self.polynomial_degree.get())
        
        # Enhanced validation
        is_valid, message, recommended_points = self.validate_probe_points_vs_degree(no_of_points, polynomial_degree)
        
        if not is_valid:
            messagebox.showerror(_("Probe Configuration Error"), message)
            return False
        

        self.probe_points = self.app.gcode.generate_and_plot_probing_points(method=self.probe_coverage_method.get(), k=no_of_points, show_plot=show_plot)
        if self.probe_points is None:
            return False
        return True
    
    def show_probe_points(self):
        """Display a popup window with the probe points."""
        if len(self.probe_points) == 0:
            messagebox.showwarning(_("Probe error"), _("No probe points found"))
            return
        popup = tkExtra.ExLabelFrame(self, text=_("Probe Points"), foreground="DarkBlue")
        popup.pack(side=TOP, fill=X)

        # Create a label for each probe point
        for point in self.probe_points:
            Label(popup.frame, text=f"Point: {point}").pack(anchor=W)

        # Add a close button
        close_button = Button(popup.frame, text=_("Close"), command=popup.destroy)
        close_button.pack(side=BOTTOM)

    def start_probing(self):
        if self.probe_points is None or len(self.probe_points) == 0:
            messagebox.showwarning(_("Probe error"), _("No probe points found"))
            return False
        
        if self.x_probe_to_tool_offset.get() != "":
            x_probe_to_tool_offset = float(self.x_probe_to_tool_offset.get())
        else:
            x_probe_to_tool_offset = 0
        if self.y_probe_to_tool_offset.get() != "":
            y_probe_to_tool_offset = float(self.y_probe_to_tool_offset.get())
        else:
            y_probe_to_tool_offset = 0
        if self.z_probe_to_tool_offset.get() != "":
            z_probe_to_tool_offset = float(self.z_probe_to_tool_offset.get())
        else:
            z_probe_to_tool_offset = 0
            
        print("Start Probing")
        mp_z_min = float(self.mp_z_min.get())
        mp_z_max = float(self.mp_z_max.get())
        
        # --- Deploy Probe ---
        safe_z = mp_z_max + z_probe_to_tool_offset
        # self.app.run([f"G0Z{safe_z:.4f}"])     # Move Z-up before Deploy Probe
        self.app.mcontrol.jog(f"Z{safe_z:.4f}")
        time.sleep(.5)
        self.app.blt_serial_send('1')          # Deploy Probe

        # --- Probing ---
        lines = self.app.gcode.probe.multi_point_scan(self.probe_points, mp_z_min, mp_z_max, x_probe_to_tool_offset, y_probe_to_tool_offset, z_probe_to_tool_offset)
        self.app.run(lines)
        print("PROBE COMMAND:")
        print("\n".join(lines))
        return True

    def quick_align_run(self):
        
        no_of_points = int(self.n_probe_points.get())
        polynomial_degree = int(self.polynomial_degree.get())
        
        # Enhanced validation
        is_valid, message, recommended_points = self.validate_probe_points_vs_degree(no_of_points, polynomial_degree)
        
        if not is_valid:
            messagebox.showerror(_("Probe Configuration Error"), message)
            return False
        
        self.stop_quick_align = False
        self._quick_align_thread()


    def _quick_align_thread(self):
        # Implementation of quick alignment in a separate thread
        success = self.generate_probe(show_plot=False)
        print("PROBE POINTS GENERATED", success)
        if success == False:
            return
        self.app.gcode.probe.start_multi_point_scan=True
        success = self.start_probing()
        print("PROBING STARTED", success)
        if success == False:
            return
        if self.check_quick_align_stop():
            print("STOPPED QUICK ALIGN WHEN PROBING 0")
            return
        
        # Start polling from main UI thread to check if probing is complete
        self.app.after(1000, self._poll_probe_status)
        
    def _poll_probe_status(self):
        if self.check_quick_align_stop():
            print("STOPPED QUICK ALIGN WHEN PROBING 1")
            return
        if self.app.gcode.probe.start_multi_point_scan:
            self.app.after(300, self._poll_probe_status)  # Check again in 100ms
        else:
            print("PROBING COMPLETED")
            self.app.after(5000, self._process_alignment_results)

    def _process_alignment_results(self):
        
        if self.check_quick_align_stop():
            print("STOPPED QUICK ALIGN WHEN PROCESSING ALIGNMENT RESULTS")
            return

        if  self.x_probe_to_tool_offset.get() != "":
            self.app.gcode.x_probe_to_tool_offset = float(self.x_probe_to_tool_offset.get())
        else:
            self.app.gcode.x_probe_to_tool_offset = 0
        if self.y_probe_to_tool_offset.get() != "":
            self.app.gcode.y_probe_to_tool_offset = float(self.y_probe_to_tool_offset.get())
        else:
            self.app.gcode.y_probe_to_tool_offset = 0
        if self.z_probe_to_tool_offset.get() != "":
            self.app.gcode.z_probe_to_tool_offset = float(self.z_probe_to_tool_offset.get())
        else:
            self.app.gcode.z_probe_to_tool_offset = 0

        polynomial_degree = int(self.polynomial_degree.get())
        bounds = self.app.gcode.surf_align_gcode(self.app.editor.getAllBlocks(), step_size=float(self.step_size.get()), degree=polynomial_degree)
        self.app.drawAfter()
        print("Bounds: ", bounds)
        print("SURF ALIGN GCODE COMPLETED")
        if bounds is None:
            messagebox.showwarning(_("Probing Error 0"), _("No probe points found 0"))
            return
        z_min = bounds.get("z_min")
        if z_min is None:
            messagebox.showwarning(_("Probing Error 1"), _("No probe points found 1"))
            return

        if z_min < float(self.z_safety_limit.get()):
            # self.app.undo()
            self.app.event_generate("<<Undo>>")
            messagebox.showwarning(_("Safety Limit Error"), _("Z-min is below the safety limit. Please adjust the Z-min safety limit."))
            return
        self.app.after(1000, self._gcode_run_command)

        
    def _gcode_run_command(self):
        
        if self.check_quick_align_stop():
            print("STOPPED QUICK ALIGN PRVENTED RUNNING GCODE")
            return
        
        self.app.run()
        print("GCODE RUN COMMAND SEND")
        
        
    def check_quick_align_stop(self):
        if self.stop_quick_align:
            self.app.gcode.probe.start_multi_point_scan = False
            self.stop_quick_align = False
            print("STOPPED QUICK ALIGN WHEN RUNNING")
            return True
        return False
        
    def quick_align_stop(self):
        self.stop_quick_align = True
# =============================================================================
# Probe Page
# =============================================================================
class SurfAlignPage(CNCRibbon.Page):
    __doc__ = _("SurfAlign configuration and probing")
    _name_ = "SurfAlign"
    _icon_ = "measure"

    # -----------------------------------------------------------------------
    # Add a widget in the widgets list to enable disable during the run
    # -----------------------------------------------------------------------
    def register(self):
        self._register(
            (ProbeTabGroup,),
            (ProbeCommonFrame, GenGcodeFrame, MultiPointProbe),
        )

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
