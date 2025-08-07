import os
import io

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


def setup_blender_scene(engrave_text, font_path, text_font_size, text_position_mm, rotation_degrees,
                        layer_height_mm, safe_height_mm, save_dir, feedrate_mm, spindle_rpm, final_height_mm, work_area_width, work_area_height, gap_distance_mm=0.2):
    blend_file_path = os.path.join(save_dir, "output.blend")
    # addon_name = "bl_ext.user_default.fabex"

    # Suppress warnings
    warnings.filterwarnings("ignore")

    # Capture print output and errors
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    
    # Create StringIO objects to capture output
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    
    sys.stdout = stdout_capture
    sys.stderr = stderr_capture

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
        bpy.context.scene.cam_machine.working_area[0] = work_area_width/1000
        bpy.context.scene.cam_machine.working_area[1] = work_area_height/1000

        # Remove all objects
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        
        temp_placeholder = "___PIPE_PLACEHOLDER___"

        # Check if text contains pipe separator for word spacing
        # First, handle special case where <|> should be treated as literal | character
        if '<|>' in engrave_text:
            # Replace <|> with a temporary placeholder, then restore after splitting
            engrave_text_processed = engrave_text.replace('<|>', temp_placeholder)
        else:
            engrave_text_processed = engrave_text
            
        if '|' in engrave_text_processed:
            # Handle pipe-separated text with spacing
            gap_distance = gap_distance_mm / 1000  # convert mm to meters
            created_objects = []
            
            # Clear selection
            bpy.ops.object.select_all(action='DESELECT')
            
            # Split the text by pipe character
            parts = engrave_text_processed.split('|')
            
            # Restore <|> placeholders back to | characters
            parts = [part.replace(temp_placeholder, '|') for part in parts]
            
            # Loop through each part and create separate text objects
            x_offset = 0
            for i, part in enumerate(parts):
                # Add text object
                bpy.ops.object.text_add(location=(x_offset, 0, 0))
                text_obj = bpy.context.object
                text_obj.data.body = part
                text_obj.data.size = text_font_size/1000
                text_obj.name = f"TextPart_{i}"
                
                # Apply font if available
                if font_path:
                    print(f"font_path for part {i}: {font_path}")
                    vect_font = bpy.data.fonts.load(font_path)
                    text_obj.data.font = vect_font
                    print(f"Font '{font_path}' loaded successfully for part {i}.")
                else:
                    print(f"No font selected for part {i}.")
                
                # Convert to mesh
                bpy.ops.object.convert(target='MESH')
                
                # Store reference
                created_objects.append(text_obj)
                
                # Update scene to get accurate bounding box
                bpy.context.view_layer.update()
                bounds = text_obj.bound_box
                width = abs(bounds[4][0] - bounds[0][0])  # X-axis width
                x_offset += width + gap_distance
            
            # Join all created mesh objects into one
            for obj in created_objects:
                obj.select_set(True)
            bpy.context.view_layer.objects.active = created_objects[0]
            bpy.ops.object.join()
            
            # Rename final object
            bpy.context.object.name = "Text"
            text_obj = bpy.context.object
            
            # Get Actual Dimensions After Conversion
            dimensions = text_obj.dimensions
            print("Actual Dimensions After Conversion (Combined):", dimensions)
            
        else:
            # Original single text object logic
            # Add text object
            bpy.ops.object.text_add(location=(0, 0, 0))
            bpy.context.object.data.size = text_font_size/1000
            
            text_obj = bpy.context.object
            # Use processed text (with <|> converted to | if needed)
            # Restore placeholder back to | character if it exists
            if '<|>' in engrave_text:
                text_obj.data.body = engrave_text_processed.replace(temp_placeholder, '|')
            else:
                text_obj.data.body = engrave_text_processed

            if font_path:
                print("font_path", font_path)
                vect_font = bpy.data.fonts.load(font_path)
                text_obj.data.font = vect_font
                print(f"Font '{font_path}' loaded successfully.")
            else:
                print("No font selected.")

            # Convert Text to Mesh to Get Accurate Dimensions
            bpy.ops.object.convert(target='MESH')
            bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='BOUNDS')

            # Get Actual Dimensions After Conversion
            dimensions = text_obj.dimensions
            print("Actual Dimensions After Conversion:", dimensions)


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
        # Get captured output
        captured_stdout = stdout_capture.getvalue()
        captured_stderr = stderr_capture.getvalue()
        
        # Close the StringIO objects
        stdout_capture.close()
        stderr_capture.close()
        
        # Restore original print output
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        warnings.resetwarnings()
        
        # # Print captured output and errors
        # if captured_stdout:
        #     print("=== CAPTURED STDOUT ===")
        #     print(captured_stdout)
        #     print("=== END STDOUT ===")
        
        # if captured_stderr:
        #     print("=== CAPTURED STDERR ===")
        #     print(captured_stderr)
        #     print("=== END STDERR ===")

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
