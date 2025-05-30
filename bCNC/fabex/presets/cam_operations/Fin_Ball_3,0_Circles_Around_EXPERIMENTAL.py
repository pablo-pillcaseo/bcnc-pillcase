import bpy
from pathlib import Path

bpy.ops.scene.cam_operation_add()

scene = bpy.context.scene
o = scene.cam_operations[scene.cam_active_operation]

o.ambient_behaviour = "AROUND"
o.ambient_radius = 0.009999999776482582
o.auto_nest = False
o.borderwidth = 50
o.carve_depth = 0.0010000000474974513
o.circle_detail = 64
o.curve_source = ""
o.cut_type = "OUTSIDE"
o.cutter_diameter = 0.003
o.cutter_length = 25.0
o.cutter_tip_angle = 60.0
o.cutter_type = "BALLNOSE"
o.distance_along_paths = 0.0002
o.distance_between_paths = 0.00015
o.dont_merge = False
o.duration = 96.3156509399414
o.feedrate = 3.0
o.filename = o.name = f"OP_{o.object_name}_{scene.cam_active_operation + 1}_{Path(__file__).stem}"
o.free_movement_height = 0.01
o.geometry_source = "OBJECT"
o.inverse = False
o.limit_curve = ""
o.material_from_model = True
o.material_origin = (0.0, 0.0, 0.0)
o.material_radius_around_model = 0.003
o.material_size = (0.20000000298023224, 0.20000000298023224, 0.10000000149011612)
o.max = (0.1325458288192749, 0.14115460216999054, -0.041229985654354095)
o.min = (0.02779383957386017, 0.014265235513448715, -0.1281193494796753)
o.min_z = -0.1281193494796753
o.min_z_from_ob = True
o.movement_type = "MEANDER"
o.object = None
o.optimize = True
o.optimize_threshold = 4.999999873689376e-05
o.parallel_angle = 0.0
o.pixsize = 9.999999747378752e-05
o.plunge_feedrate = 30.0
o.protect_vertical = True
o.render_all = True
o.skin = 0.0
o.slice_detail = 0.0010000000474974513
o.source_image_name = ""
o.source_image_offset = (0.0, 0.0, 0.0)
o.source_image_scale_z = 1.0
o.source_image_size_x = 0.10000000149011612
o.spindle = 30000.0
o.stay_low = True
o.stepdown = 0.009999999776482582
o.strategy = "CIRCLES"
o.testing = 0
o.update_offset_image_tag = False
o.update_silhouette_tag = True
o.update_z_buffer_image_tag = False
o.use_layers = False
o.use_limit_curve = False
o.waterline_fill = True
