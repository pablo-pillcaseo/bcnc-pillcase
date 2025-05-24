import os

# Redirect Blender's configuration, data, and scripts to your custom paths
os.environ["BLENDER_USER_CONFIG"] = r""
os.environ["BLENDER_USER_SCRIPTS"] = r""
os.environ["BLENDER_USER_DATAFILES"] = r""

import bpy
import sys
import warnings
from scipy.spatial import ConvexHull
import numpy as np
import matplotlib.pyplot as plt
import re
import math
import winreg
import mathutils

# import fabex addon
import fabex

# Register the addon
if hasattr(fabex, "register"):
    fabex.register()
else:
    print("No register() function found in addon.")


def resolve_windows_font_path(display_name):
    """
    Given a display name like 'Arial' or 'Bahnschrift SemiLight SemiConde',
    this function returns the actual font file path.
    """
    display_name = display_name.lower()
    fonts_dir = os.path.join(os.environ["WINDIR"], "Fonts")

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
            for i in range(winreg.QueryInfoKey(key)[1]):
                name, font_file, _ = winreg.EnumValue(key, i)
                if display_name in name.lower():
                    return os.path.join(fonts_dir, font_file)
    except Exception as e:
        print("🛑 Registry access failed:", e)

    return None


def resolve_font_path(font_name):
    font_name = font_name.strip().lower()
    fonts_dir = os.path.join(os.environ["WINDIR"], "Fonts")

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
            for i in range(0, winreg.QueryInfoKey(key)[1]):
                display_name, font_file, _ = winreg.EnumValue(key, i)
                if font_name in display_name.lower():
                    return os.path.join(fonts_dir, font_file)
    except Exception as e:
        print(f"Registry access failed: {e}")

    return None


def setup_blender_scene(engrave_text, text_font, text_width_mm, text_height_mm, text_position_mm, rotation_degrees,
                        layer_height_mm, safe_height_mm, save_dir, feedrate_mm, spindle_rpm, final_height_mm):
    blend_file_path = os.path.join(save_dir, "output.blend")
    # addon_name = "bl_ext.user_default.fabex"

    # Suppress warnings
    warnings.filterwarnings("ignore")

    # Suppress print output
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = open(os.devnull, 'w')
    sys.stderr = open(os.devnull, 'w')

    try:
        # # Enable Addon
        # if addon_name not in bpy.context.preferences.addons:
        #     bpy.ops.preferences.addon_enable(module=addon_name)
        #     print(f"Addon '{addon_name}' enabled successfully.")
        # else:
        #     print(f"Addon '{addon_name}' is already enabled.")

        # Set Render Engine
        bpy.context.scene.render.engine = 'FABEX_RENDER'
        bpy.context.scene.interface.level = '2'
        bpy.context.scene.cam_machine.feedrate_default = 1.51

        # Set units to millimeters
        # bpy.context.scene.unit_settings.system = 'METRIC'
        # bpy.context.scene.unit_settings.scale_length = 0.001
        bpy.context.scene.unit_settings.length_unit = 'MILLIMETERS'  # Set units to millimeters

        bpy.context.scene.cam_machine.output_tool_change = False  # Disable tool change command
        bpy.context.scene.cam_machine.eval_splitting = False  # Disable splitting g-code for large files

        # Remove all objects
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)

        # Add text object
        bpy.ops.object.text_add(location=(0, 0, 0))
        text_obj = bpy.context.object
        text_obj.data.body = engrave_text

        if text_font:
            font_path = resolve_windows_font_path(text_font)
            print("font_path", font_path)
            if font_path:
                vect_font = bpy.data.fonts.load(font_path)
                text_obj.data.font = vect_font
                print(f"Font '{text_font}' loaded successfully.")
            else:
                print(f"Font '{text_font}' not found.")
        else:
            print("No font selected.")

        # Convert Text to Mesh to Get Accurate Dimensions
        bpy.ops.object.convert(target='MESH')
        bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='BOUNDS')

        # Get Actual Dimensions After Conversion
        dimensions = text_obj.dimensions
        print("Actual Dimensions After Conversion:", dimensions)

        # Scale text object to fit within 50mm x 100mm while keeping aspect ratio
        scale_factor = min(text_width_mm / (dimensions.x * 1000), text_height_mm / (dimensions.y * 1000))
        text_obj.scale = (scale_factor, scale_factor, scale_factor)
        text_pos_meters = (text_position_mm[0] / 1000, text_position_mm[1] / 1000, text_position_mm[2] / 1000)
        # Move text object to a new location (e.g., (0, 0, 1))
        text_obj.location = text_pos_meters
        text_obj.rotation_euler = (0, 0, math.radians(rotation_degrees))  # Convert to radians

        bpy.ops.object.origin_set(type='ORIGIN_CENTER_OF_MASS', center='MEDIAN')
        # bpy.context.object.location[1] = 0
        
        # Move the object along its local Y axis by that distance
        obj = bpy.context.object
        point = mathutils.Vector((text_position_mm[0], text_position_mm[1], text_position_mm[2]))/1000  # Replace with your target point
        local_y_world = obj.matrix_world.to_quaternion() @ mathutils.Vector((0, 1, 0))
        vec_to_point = point - obj.location
        signed_distance = vec_to_point.dot(local_y_world)
        obj.location += local_y_world * signed_distance
    
        bpy.ops.scene.cam_operation_add()

        bpy.ops.wm.save_as_mainfile(filepath=blend_file_path)

        print(f"COMPLETED! Text is properly sized to {text_width_mm}mm x {text_height_mm}mm")

        bpy.context.scene.cam_operations[0].cut_type = 'ONLINE'
        bpy.context.scene.cam_operations[0].stepdown = layer_height_mm / 1000  # layer height
        bpy.context.scene.cam_operations[0].movement.free_height = safe_height_mm / 1000  # Safe height
        bpy.context.scene.cam_operations[0].outlines_count = 2
        bpy.context.scene.cam_operations[0].feedrate = feedrate_mm / 1000
        bpy.context.scene.cam_operations[0].spindle_rpm = spindle_rpm
        bpy.context.scene.cam_operations[0].output_trailer = True
        bpy.context.scene.cam_operations[0].gcode_trailer = f"G00 X0Y0Z{final_height_mm}"


        bpy.ops.object.calculate_cam_path()

        # bpy.ops.wm.save_as_mainfile(filepath=blend_file_path)

        gcode_file_path = os.path.join(save_dir, "Op_Text_1.tap")

        print("COMPLETED! GCODE is generated", gcode_file_path)

        # Instead of closing, reset the scene
        bpy.ops.wm.read_factory_settings(use_empty=True)

        # print("COMPLETED! GCODE is generated", blend_file_path)

    finally:
        sys.stdout.close()
        sys.stderr.close()
        # Restore original print output
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        warnings.resetwarnings()

    return gcode_file_path


import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D


def build_vandermonde(X, Y, degree):
    """
    Create the design matrix (Vandermonde) for 2D polynomial fitting.
    """
    terms = []
    for i in range(degree + 1):
        for j in range(degree + 1 - i):
            terms.append((X ** i) * (Y ** j))
    return np.vstack(terms).T


def fit_polynomial_surface_numpy(points, degree=2):
    """
    Fit a polynomial surface using numpy only.

    :param points: Nx3 array of [X, Y, Z]
    :param degree: Degree of polynomial surface
    :return: Coefficients, meshgrid, Z_pred
    """
    points = np.array(points)
    X, Y, Z = points[:, 0], points[:, 1], points[:, 2]

    A = build_vandermonde(X, Y, degree)
    coeffs, *_ = np.linalg.lstsq(A, Z, rcond=None)  # least squares solution

    # Grid for plotting
    x_range = np.linspace(X.min(), X.max(), 50)
    y_range = np.linspace(Y.min(), Y.max(), 50)
    X_grid, Y_grid = np.meshgrid(x_range, y_range)

    A_grid = build_vandermonde(X_grid.ravel(), Y_grid.ravel(), degree)
    Z_pred = A_grid @ coeffs
    Z_grid = Z_pred.reshape(X_grid.shape)

    return coeffs, X_grid, Y_grid, Z_grid


def plot_surface(points, X_grid, Y_grid, Z_grid):
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')

    points = np.array(points)
    ax.scatter(points[:, 0], points[:, 1], points[:, 2], color='r', label='Data')
    ax.plot_surface(X_grid, Y_grid, Z_grid, cmap='viridis', alpha=0.6)

    ax.set_xlabel('X')
    ax.set_ylabel('Y')
    ax.set_zlabel('Z')
    ax.set_title("Fitted Polynomial Surface")
    plt.axis("equal")
    plt.legend()
    plt.show()


def calculate_z_from_poly(X, Y, coeffs):
    """
    Calculate Z values from X and Y using the polynomial coefficients.

    :param X: Array of X values
    :param Y: Array of Y values
    :param coeffs: Polynomial coefficients
    :return: Calculated Z values
    """
    degree = int(np.sqrt(len(coeffs))) - 1  # Assuming a square polynomial
    Z = np.zeros_like(X)
    index = 0
    for i in range(degree + 1):
        for j in range(degree + 1 - i):
            Z += coeffs[index] * (X ** i) * (Y ** j)
            index += 1
    return Z
