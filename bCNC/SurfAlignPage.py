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
            _("Set initial probe feed rate for tool change and calibration"),
        )
        self.addWidget(GenGcodeFrame.engraveText)

        # Font selection dropdown
        row += 1
        col = 0
        Label(lframe(), text=_("Font:")).grid(row=row, column=col, sticky=E)
        col += 1

        font_list = self.get_installed_font_names()

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

        # ----
        # Size (Height, Width)
        row += 1
        col = 0
        Label(lframe(), text=_("Size (H, W):")).grid(row=row, column=col, sticky=E)
        col += 1
        self.height = tkExtra.FloatEntry(
            lframe(), background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.height.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.height, _("Engrave Text Height"))

        col += 1
        self.width = tkExtra.FloatEntry(
            lframe(), background=tkExtra.GLOBAL_CONTROL_BACKGROUND
        )
        self.width.grid(row=row, column=col, sticky=EW)
        tkExtra.Balloon.set(
            self.width, _("Engrave Text Width"))
        
        # ----
        # Pos (X, Y)
        row, col = row + 1, 0
        Label(lframe(), text=_("Pos:")).grid(row=row, column=col, sticky=E)

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
        self.height.set(Utils.getFloat("SurfAlign", "height"))
        self.width.set(Utils.getFloat("SurfAlign", "width"))
        self.posX.set(Utils.getFloat("SurfAlign", "posX"))
        self.posY.set(Utils.getFloat("SurfAlign", "posY"))
        self.rotation.set(Utils.getFloat("SurfAlign", "rotation"))
        self.feedrate.set(Utils.getFloat("SurfAlign", "feedrate"))
        self.spindleRPM.set(Utils.getFloat("SurfAlign", "spindleRPM"))
        self.engraveDepth.set(Utils.getFloat("SurfAlign", "engraveDepth"))
        self.layerHeight.set(Utils.getFloat("SurfAlign", "layerHeight"))
        self.safeHeight.set(Utils.getFloat("SurfAlign", "safeHeight"))
        self.finalHeight.set(Utils.getFloat("SurfAlign", "finalHeight"))
        
    def generateGcode(self):
        print("Generate Gcode")
        engrave_text = self.engraveText.get()
        if self.font_var.get() == "":
            text_font = None
        else:
            text_font = self.font_var.get()
        text_height_mm, text_width_mm = float(self.height.get()), float(self.width.get())
        text_position_mm = (float(self.posX.get()), float(self.posY.get()), -float(self.engraveDepth.get()))
        layer_height_mm = float(self.layerHeight.get())
        safe_height_mm = float(self.safeHeight.get())
        final_height_mm = float(self.finalHeight.get())
        save_dir = os.path.dirname(__file__)
        rotation_degrees = float(self.rotation.get())
        feedrate_mm = float(self.feedrate.get())
        spindle_rpm = float(self.spindleRPM.get())
        gcode_file_path = setup_blender_scene(engrave_text, text_font, text_width_mm, text_height_mm, text_position_mm, rotation_degrees, layer_height_mm, safe_height_mm, save_dir, feedrate_mm, spindle_rpm, final_height_mm)
        
        
        print("Generated Gcode file path:", gcode_file_path)
        self.app.load(gcode_file_path)
        print("Loaded Gcode file:", self.app.gcode.filename)




    # # -----------------------------------------------------------------------
    def saveConfig(self):
        Utils.setStr("SurfAlign", "engraveText", self.engraveText.get())
        Utils.setStr("SurfAlign", "textFont", self.font_var.get())
        Utils.setFloat("SurfAlign", "height", self.height.get())
        Utils.setFloat("SurfAlign", "width", self.width.get())
        Utils.setFloat("SurfAlign", "posX", self.posX.get())
        Utils.setFloat("SurfAlign", "posY", self.posY.get())
        Utils.setFloat("SurfAlign", "rotation", self.rotation.get())
        Utils.setFloat("SurfAlign", "feedrate", self.feedrate.get())
        Utils.setFloat("SurfAlign", "spindleRPM", self.spindleRPM.get())
        Utils.setFloat("SurfAlign", "engraveDepth", self.engraveDepth.get())
        Utils.setFloat("SurfAlign", "layerHeight", self.layerHeight.get())
        Utils.setFloat("SurfAlign", "safeHeight", self.safeHeight.get())
        Utils.setFloat("SurfAlign", "finalHeight", self.finalHeight.get())




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
        self.n_probe_points = tkExtra.FloatEntry(
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
        self.n_probe_points.set(Utils.getFloat("SurfAlign", "n_probe_points"))
        self.x_probe_to_tool_offset.set(Utils.getFloat("SurfAlign", "x_probe_to_tool_offset"))
        self.y_probe_to_tool_offset.set(Utils.getFloat("SurfAlign", "y_probe_to_tool_offset"))
        self.z_probe_to_tool_offset.set(Utils.getFloat("SurfAlign", "z_probe_to_tool_offset"))
        self.z_safety_limit.set(Utils.getFloat("SurfAlign", "z_safety_limit"))
        
    def saveConfig(self):
        Utils.setFloat("SurfAlign", "mp_z_min", self.mp_z_min.get())
        Utils.setFloat("SurfAlign", "mp_z_max", self.mp_z_max.get())
        Utils.setFloat("SurfAlign", "n_probe_points", self.n_probe_points.get())
        Utils.setFloat("SurfAlign", "x_probe_to_tool_offset", self.x_probe_to_tool_offset.get())
        Utils.setFloat("SurfAlign", "y_probe_to_tool_offset", self.y_probe_to_tool_offset.get())
        Utils.setFloat("SurfAlign", "z_probe_to_tool_offset", self.z_probe_to_tool_offset.get())
        Utils.setFloat("SurfAlign", "z_safety_limit", self.z_safety_limit.get())
        
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
        bounds = self.app.gcode.surf_align_gcode(self.app.editor.getSelectedBlocks())
        self.app.drawAfter()
    
    def generate_probe(self, show_plot=True):
        
        no_of_points = int(float(self.n_probe_points.get()))
        if no_of_points < 3:
            messagebox.showwarning(_("Probe error"), _("Number of probe points must be greater than 2"))
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
            self.app.after(2000, self._process_alignment_results)

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

        bounds = self.app.gcode.surf_align_gcode(self.app.editor.getAllBlocks())
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
