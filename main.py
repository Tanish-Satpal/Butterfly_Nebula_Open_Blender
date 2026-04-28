import bpy
import os
import math
import random
import tempfile
from mathutils import Vector
from mathutils import noise as mnoise

try:
    import openvdb
    HAVE_OPENVDB = True
except ImportError:
    HAVE_OPENVDB = False

CACHE_DIR = "//cache"

NEBULA_COLLECTION_NAME = "ButterflyNebula"
SRC_POINTS_COLLECTION_NAME = "NB_SourcePoints"
VOLUME_COLLECTION_NAME = "NB_Volumes"
CAM_COLLECTION_NAME = "NB_Cameras"
LIGHT_COLLECTION_NAME = "NB_Lights"
DEBUG_COLLECTION_NAME = "NB_Debug"

POINTCLOUD_NAME = "NB_PointCloud_Main"

VOL_ION_NAME = "VOL_IonGas"
VOL_DUST_NAME = "VOL_Dust"
VOL_HAZE_NAME = "VOL_Haze"

MAT_ION_NAME = "MAT_IonGas"
MAT_DUST_NAME = "MAT_Dust"
MAT_HAZE_NAME = "MAT_Haze"

CAMERA_NAME = "NB_RenderCamera"

STAGE_PREVIEW = "preview"
STAGE_LOOKDEV = "lookdev"
STAGE_FINAL = "final"


def log(msg: str):
    print(f"[ButterflyNebula] {msg}")


def ensure_collection(name, parent=None):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
    if parent is None:
        parent = bpy.context.scene.collection
    if col.name not in parent.children:
        try:
            parent.children.link(col)
        except RuntimeError:
            pass
    return col


def remove_object_if_exists(name):
    obj = bpy.data.objects.get(name)
    if obj is not None:
        bpy.data.objects.remove(obj, do_unlink=True)


def remove_material_if_exists(name):
    mat = bpy.data.materials.get(name)
    if mat is not None:
        bpy.data.materials.remove(mat, do_unlink=True)


def remove_volume_if_exists(name):
    vol = bpy.data.volumes.get(name)
    if vol is not None:
        bpy.data.volumes.remove(vol, do_unlink=True)


def remove_pointcloud_if_exists(name):
    pc = bpy.data.pointclouds.get(name)
    if pc is not None:
        bpy.data.pointclouds.remove(pc, do_unlink=True)


def ensure_cache_dir():
    candidates = []

    try:
        candidates.append(bpy.path.abspath(CACHE_DIR))
    except Exception:
        pass

    try:
        if bpy.data.filepath:
            candidates.append(os.path.join(os.path.dirname(bpy.data.filepath), "cache"))
    except Exception:
        pass

    candidates.append(os.path.join(tempfile.gettempdir(), "blender_butterfly_nebula_cache"))

    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            test_file = os.path.join(path, ".write_test")
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test_file)
            log(f"Using cache directory: {path}")
            return path
        except Exception as e:
            log(f"Cache dir not writable: {path} ({e})")

    raise RuntimeError("No writable cache directory available.")


def scene_bootstrap():
    scene = bpy.context.scene

    nb_col = ensure_collection(NEBULA_COLLECTION_NAME)
    ensure_collection(SRC_POINTS_COLLECTION_NAME, parent=nb_col)
    ensure_collection(VOLUME_COLLECTION_NAME, parent=nb_col)
    ensure_collection(CAM_COLLECTION_NAME, parent=nb_col)
    ensure_collection(LIGHT_COLLECTION_NAME, parent=nb_col)
    ensure_collection(DEBUG_COLLECTION_NAME, parent=nb_col)

    scene.render.engine = 'CYCLES'
    c = scene.cycles

    if hasattr(c, "use_adaptive_sampling"):
        c.use_adaptive_sampling = True
    if hasattr(c, "adaptive_threshold"):
        c.adaptive_threshold = 0.01
    if hasattr(c, "use_preview_denoising"):
        c.use_preview_denoising = True
    if hasattr(c, "use_denoising"):
        c.use_denoising = True

    c.max_bounces = 12
    c.diffuse_bounces = 2
    c.glossy_bounces = 2
    c.transmission_bounces = 8
    c.transparent_max_bounces = 16

    if hasattr(c, "volume_step_rate"):
        c.volume_step_rate = 1.0
    if hasattr(c, "volume_max_steps"):
        c.volume_max_steps = 256

    scene.render.image_settings.file_format = 'PNG'
    scene.render.resolution_x = 2160
    scene.render.resolution_y = 2160
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False

    if not scene.world:
        scene.world = bpy.data.worlds.new("NebulaWorld")
    world = scene.world
    world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()

    n_bg = nt.nodes.new("ShaderNodeBackground")
    n_out = nt.nodes.new("ShaderNodeOutputWorld")
    n_bg.inputs["Color"].default_value = (0.006, 0.008, 0.014, 1.0)
    n_bg.inputs["Strength"].default_value = 0.01
    nt.links.new(n_bg.outputs["Background"], n_out.inputs["Surface"])

    view = scene.view_settings
    if hasattr(view, "view_transform"):
        try:
            view.view_transform = "AgX"
        except Exception:
            pass
    if hasattr(view, "look"):
        try:
            view.look = "None"
        except Exception:
            pass
    if hasattr(view, "exposure"):
        view.exposure = -0.35
    if hasattr(view, "gamma"):
        view.gamma = 1.0

    log("Scene bootstrap complete.")


def build_source_pointclouds():
    src_col = ensure_collection(SRC_POINTS_COLLECTION_NAME, parent=ensure_collection(NEBULA_COLLECTION_NAME))

    remove_object_if_exists(POINTCLOUD_NAME)
    remove_pointcloud_if_exists(POINTCLOUD_NAME)

    mesh_name = POINTCLOUD_NAME + "_Mesh"
    old_mesh = bpy.data.meshes.get(mesh_name)
    if old_mesh is not None:
        bpy.data.meshes.remove(old_mesh, do_unlink=True)

    mesh = bpy.data.meshes.new(mesh_name)
    obj = bpy.data.objects.new(POINTCLOUD_NAME, mesh)
    src_col.objects.link(obj)

    for attr_name in ("radius", "emit_w", "dust_w", "haze_w", "filament", "shell", "temp_k"):
        if attr_name not in mesh.attributes:
            mesh.attributes.new(attr_name, type='FLOAT', domain='POINT')

    obj.display_type = 'WIRE'
    obj.hide_render = True

    log("Source mesh-point container created with point-domain attributes.")
    return obj


def generate_butterfly_fields(src_obj, num_points=5000):
    mesh = src_obj.data
    mesh.clear_geometry()

    verts = []

    lobe_length = 7.0
    lobe_radius = 2.2
    waist_radius = 0.14

    radius_vals = []
    emit_vals = []
    dust_vals = []
    haze_vals = []
    filament_vals = []
    shell_vals = []
    temp_vals = []

    for i in range(num_points):
        side = -1.0 if random.random() < 0.5 else 1.0
        t = random.random()

        # asymmetry per side so the two lobes are related, not cloned
        side_len = lobe_length * (0.98 if side < 0.0 else 1.02)
        side_rad = lobe_radius * (1.03 if side < 0.0 else 0.97)
        side_bow = 0.64 if side < 0.0 else 0.60

        x = side * (waist_radius + t * side_len)
        wing_taper = (1.0 - t) ** 1.45
        x *= 1.0 + 0.26 * (1.0 - wing_taper)

        flare = waist_radius + (side_rad - waist_radius) * (t ** 0.52)

        # bias samples toward a thin shell instead of filling the whole lobe
        shell_center = 0.92 + 0.18 * random.random()
        shell_thickness = 0.11 + 0.16 * random.random()
        r_shell = flare * shell_center
        r = max(0.0, r_shell + (random.random() - 0.5) * flare * shell_thickness)

        ang = random.random() * math.tau
        y = r * math.cos(ang)
        z = r * math.sin(ang)

        bow = (1.0 - t) ** 1.55
        z += bow * side_bow * (1.0 if random.random() > 0.5 else -1.0)
        z += 0.26 * math.copysign(t ** 1.25, x)

        axis_noise = mnoise.fractal(Vector((y * 0.45, z * 0.45, t * 2.3)), 2.2, 2.0, 3)
        x += axis_noise * (0.55 * t)

        n = mnoise.fractal(Vector((x * 0.42, y * 1.15, z * 1.15)), 2.6, 2.0, 3)
        y *= 1.0 + 0.18 * n
        z *= 1.0 + 0.08 * n


        verts.append((x, y, z))

        radial = math.sqrt(y * y + z * z)
        dist = math.sqrt(x * x + y * y + z * z)

        shell_band = abs(radial - flare) / max(0.001, flare)
        local_shell = max(0.0, 1.0 - shell_band / 0.22)
        local_shell = (local_shell ** 1.2) * (0.55 + 0.45 * wing_taper)

        # suppress density along the lobe axis so the centre reads as hollow
        axis_hollow = max(0.0, 1.0 - radial / max(0.001, flare * 0.58))
        axis_hollow = axis_hollow ** 1.8

        local_fil = max(0.0, 1.0 - abs(radial - flare * 0.82) / max(0.001, flare * 0.34))
        local_fil = (local_fil ** 1.35) * (0.35 + 0.65 * wing_taper)

        waist_factor = max(0.0, 1.0 - abs(x) / 1.0) * max(0.0, 1.0 - radial / (waist_radius * 2.3))

        haze_factor = max(0.0, 1.0 - dist / (lobe_length * 1.18))
        waist_hollow = max(0.0, 1.0 - abs(x) / 0.42)  # only very close to waist
        haze_factor = (haze_factor ** 1.7) * (1.0 - 0.35 * axis_hollow) * (1.0 - 0.30 * waist_hollow)

        # brighter inner spine near the axis, fades toward the shell
        axis_spine = max(0.0, 1.0 - radial / max(0.001, flare * 0.82))
        axis_spine = axis_spine ** 1.6

        radius_vals.append(0.024 + 0.018 * (1.0 - t) + 0.010 * random.random())
        emit_vals.append(0.14 + 0.42 * (1.0 - t) + 0.72 * local_shell)
        dust_vals.append(max(0.0, 0.55 * waist_factor + 0.03 * random.random()))
        haze_vals.append(
            0.55 * haze_factor
            + 0.90 * local_shell
            + 0.30 * local_fil
            + 0.18 * axis_spine  # extra brightness only near the spine
        )
        filament_vals.append(local_fil)
        shell_vals.append(local_shell)

        # hotter near core, cooler outward, with slight turbulence
        temp_norm = max(0.0, min(1.0, 0.92 - 0.58 * t + 0.10 * local_shell + (random.random() - 0.5) * 0.10))
        temp_vals.append(temp_norm)

    halo_count = max(int(num_points * 0.06), 220)
    base_count = len(verts)

    for j in range(halo_count):
        idx = random.randrange(base_count)
        x0, y0, z0 = verts[idx]

        dir_angle = random.random() * math.tau
        spread = lobe_radius * (0.22 + 0.50 * random.random())
        y_off = spread * math.cos(dir_angle)
        z_off = spread * math.sin(dir_angle)

        # stretch halos outward along the lobe instead of making puffy shells
        flow = (0.12 + 0.24 * random.random()) * lobe_length
        x_off = math.copysign(flow, x0)

        flow_noise = mnoise.fractal(Vector((x0 * 0.4, y0 * 0.4, z0 * 0.4)), 2.1, 2.0, 2)
        hx = x0 + x_off + 0.18 * flow_noise * (1.0 + 0.6 * random.random())
        hy = y0 + y_off * (0.85 + 0.3 * random.random())
        hz = z0 + z_off * (0.85 + 0.3 * random.random())

        verts.append((hx, hy, hz))

        radius_vals.append(radius_vals[idx] * 0.95)
        emit_vals.append(emit_vals[idx] * 0.10)
        dust_vals.append(dust_vals[idx] * 0.05)
        haze_vals.append(min(1.0, 0.25 + haze_vals[idx] * 1.10))
        filament_vals.append(0.12)
        shell_vals.append(0.15)
        temp_vals.append(max(0.12, temp_vals[idx] * (0.55 + 0.10 * random.random())))

    mesh.from_pydata(verts, [], [])
    mesh.update()

    for attr_name in ("radius", "emit_w", "dust_w", "haze_w", "filament", "shell", "temp_k"):
        if attr_name not in mesh.attributes:
            mesh.attributes.new(attr_name, type='FLOAT', domain='POINT')

    mesh.attributes["radius"].data.foreach_set("value", radius_vals)
    mesh.attributes["emit_w"].data.foreach_set("value", emit_vals)
    mesh.attributes["dust_w"].data.foreach_set("value", dust_vals)
    mesh.attributes["haze_w"].data.foreach_set("value", haze_vals)
    mesh.attributes["filament"].data.foreach_set("value", filament_vals)
    mesh.attributes["shell"].data.foreach_set("value", shell_vals)
    mesh.attributes["temp_k"].data.foreach_set("value", temp_vals)

    log(f"Generated butterfly source field with {len(verts)} vertices ({num_points} core + {halo_count} halo).")


def write_vdb_files(src_obj, ion_path, dust_path, haze_path, voxel_size=0.05):
    if not HAVE_OPENVDB:
        log("openvdb Python module not available; skipping VDB writing.")
        log("TODO: External VDB generation required.")
        log(f"Expected outputs:\n  {ion_path}\n  {dust_path}\n  {haze_path}")
        return False

    mesh = src_obj.data
    verts = mesh.vertices
    count = len(verts)

    radius_vals = [0.0] * count
    emit_vals = [0.0] * count
    dust_vals = [0.0] * count
    haze_vals = [0.0] * count
    filament_vals = [0.0] * count
    shell_vals = [0.0] * count
    temp_vals = [0.0] * count

    mesh.attributes["radius"].data.foreach_get("value", radius_vals)
    mesh.attributes["emit_w"].data.foreach_get("value", emit_vals)
    mesh.attributes["dust_w"].data.foreach_get("value", dust_vals)
    mesh.attributes["haze_w"].data.foreach_get("value", haze_vals)
    mesh.attributes["filament"].data.foreach_get("value", filament_vals)
    mesh.attributes["shell"].data.foreach_get("value", shell_vals)
    mesh.attributes["temp_k"].data.foreach_get("value", temp_vals)

    def make_grid(name):
        g = openvdb.FloatGrid()
        g.name = name
        if hasattr(g, "background"):
            g.background = 0.0
        if hasattr(openvdb, "createLinearTransform"):
            try:
                g.transform = openvdb.createLinearTransform(voxel_size)
            except Exception:
                pass
        return g

    ion_density = make_grid("density")
    ion_temp = make_grid("temperature")
    dust_density = make_grid("density")
    haze_density = make_grid("density")
    haze_temp = make_grid("temperature")

    def world_to_index(grid, p):
        if hasattr(grid, "worldToIndex"):
            return grid.worldToIndex((p.x, p.y, p.z))
        if hasattr(grid, "transform") and hasattr(grid.transform, "worldToIndex"):
            return grid.transform.worldToIndex((p.x, p.y, p.z))
        return (p.x / voxel_size, p.y / voxel_size, p.z / voxel_size)

    def get_value(grid, coord):
        if hasattr(grid, "getValue"):
            return grid.getValue(coord)
        if hasattr(grid, "getAccessor"):
            try:
                return grid.getAccessor().getValue(coord)
            except Exception:
                return 0.0
        return 0.0

    def set_value(grid, coord, value):
        if hasattr(grid, "setValue"):
            grid.setValue(coord, value)
            return
        if hasattr(grid, "getAccessor"):
            grid.getAccessor().setValueOn(coord, value)
            return

    for i, v in enumerate(verts):
        p = src_obj.matrix_world @ v.co

        radius = max(0.008, radius_vals[i])
        emit_w = emit_vals[i]
        dust_w = dust_vals[i]
        haze_w = haze_vals[i]
        filament = filament_vals[i]
        shell = shell_vals[i]
        temp = temp_vals[i]

        ijk = world_to_index(ion_density, p)
        cx, cy, cz = int(round(ijk[0])), int(round(ijk[1])), int(round(ijk[2]))
        vr = max(1, int(math.ceil((radius * 1.35) / voxel_size)))

        # concentrate ion emission into shell/filament walls
        ion_base = emit_w * (0.16 + 0.28 * shell) * (0.22 + 0.18 * filament)

        # keep dust subtle so the nebula feels luminous instead of muddy
        dust_base = dust_w * (0.55 - 0.20 * shell)

        # haze is the main carrier of the soft aesthetic glow
        haze_base = 0.62 * haze_w + 0.82 * shell + 0.20 * filament

        for dx in range(-vr, vr + 1):
            for dy in range(-vr, vr + 1):
                for dz in range(-vr, vr + 1):
                    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
                    if dist > vr:
                        continue
                    u = dist / max(1e-6, vr)
                    falloff = max(0.0, (1.0 - u * u) ** 2)
                    c = (cx + dx, cy + dy, cz + dz)

                    if ion_base > 0.0:
                        old = get_value(ion_density, c)
                        set_value(ion_density, c, old + ion_base * falloff)
                        old_t = get_value(ion_temp, c)
                        set_value(ion_temp, c, max(old_t, temp))

                    if dust_base > 0.0:
                        old = get_value(dust_density, c)
                        set_value(dust_density, c, old + dust_base * falloff)

                    if haze_base > 0.0:
                        old_h = get_value(haze_density, c)
                        set_value(haze_density, c, old_h + haze_base * falloff)
                        old_ht = get_value(haze_temp, c)
                        # store max temp so hot regions stay hot
                        set_value(haze_temp, c, max(old_ht, temp))

                        # # Color weights follow the same halo spread
                        # old_b = get_value(haze_blue, c)
                        # old_tl = get_value(haze_teal, c)
                        # old_wm = get_value(haze_warm, c)
                        # set_value(haze_blue, c, old_b + blue_w * falloff)
                        # set_value(haze_teal, c, old_tl + teal_w * falloff)
                        # set_value(haze_warm, c, old_wm + warm_w * falloff)

    if hasattr(openvdb, "write"):
        openvdb.write(ion_path, grids=[ion_density, ion_temp])
        openvdb.write(dust_path, grids=[dust_density])
        openvdb.write(haze_path, grids=[haze_density, haze_temp])
        log("VDB files written.")
        return True

    log("openvdb binding has no write() function; skipping VDB export.")
    return False


def import_vdb_as_volume_object(obj_name, filepath, collection):
    remove_object_if_exists(obj_name)
    remove_volume_if_exists(obj_name)

    if not os.path.exists(filepath):
        vol_data = bpy.data.volumes.new(obj_name)
        obj = bpy.data.objects.new(obj_name, vol_data)
        collection.objects.link(obj)
        obj.location = (0.0, 0.0, 0.0)
        return obj

    if hasattr(bpy.data.volumes, "load"):
        vol_data = bpy.data.volumes.load(filepath)
        vol_data.name = obj_name
        obj = bpy.data.objects.new(obj_name, vol_data)
        collection.objects.link(obj)
        obj.location = (0.0, 0.0, 0.0)
        return obj

    if hasattr(bpy.ops.object, "volume_import"):
        before = set(bpy.data.objects.keys())
        bpy.ops.object.volume_import(filepath=filepath)
        after = set(bpy.data.objects.keys())
        new_names = list(after - before)
        if not new_names:
            raise RuntimeError(f"volume_import did not create an object for {filepath}")
        obj = bpy.data.objects[new_names[0]]
        obj.name = obj_name
        if obj.data:
            obj.data.name = obj_name
        for c in list(obj.users_collection):
            c.objects.unlink(obj)
        collection.objects.link(obj)
        obj.location = (0.0, 0.0, 0.0)
        return obj

    log(f"No direct volume load API available for {filepath}; creating placeholder volume {obj_name}")
    vol_data = bpy.data.volumes.new(obj_name)
    obj = bpy.data.objects.new(obj_name, vol_data)
    collection.objects.link(obj)
    obj.location = (0.0, 0.0, 0.0)
    return obj


def import_volumes(ion_path, dust_path, haze_path):
    vol_col = ensure_collection(VOLUME_COLLECTION_NAME, parent=ensure_collection(NEBULA_COLLECTION_NAME))
    ion_obj = import_vdb_as_volume_object(VOL_ION_NAME, ion_path, vol_col)
    dust_obj = import_vdb_as_volume_object(VOL_DUST_NAME, dust_path, vol_col)
    haze_obj = import_vdb_as_volume_object(VOL_HAZE_NAME, haze_path, vol_col)
    return ion_obj, dust_obj, haze_obj


def build_materials(ion_obj, dust_obj, haze_obj):
    remove_material_if_exists(MAT_ION_NAME)
    remove_material_if_exists(MAT_DUST_NAME)
    remove_material_if_exists(MAT_HAZE_NAME)

    # ION GAS MATERIAL (emissive)
    mat_ion = bpy.data.materials.new(MAT_ION_NAME)
    mat_ion.use_nodes = True
    nt = mat_ion.node_tree
    nt.nodes.clear()

    n_out = nt.nodes.new("ShaderNodeOutputMaterial")
    n_info = nt.nodes.new("ShaderNodeVolumeInfo")
    n_map = nt.nodes.new("ShaderNodeMapRange")
    n_ramp = nt.nodes.new("ShaderNodeValToRGB")
    n_principled = nt.nodes.new("ShaderNodeVolumePrincipled")

    n_info.location = (-700, 0)
    n_map.location = (-480, 0)
    n_ramp.location = (-260, 140)
    n_principled.location = (20, 60)
    n_out.location = (260, 60)

    n_map.inputs["From Min"].default_value = 0.0
    n_map.inputs["From Max"].default_value = 2.0
    n_map.inputs["To Min"].default_value = 0.0
    n_map.inputs["To Max"].default_value = 1.0
    n_map.clamp = True

    ramp = n_ramp.color_ramp
    while len(ramp.elements) > 2:
        ramp.elements.remove(ramp.elements[-1])
    ramp.elements[0].position = 0.0
    ramp.elements[0].color = (0.86, 0.78, 0.96, 1.0)
    ramp.elements[1].position = 1.0
    ramp.elements[1].color = (0.42, 0.56, 0.96, 1.0)

    if "Color" in n_principled.inputs:
        n_principled.inputs["Color"].default_value = (0.7, 0.8, 1.0, 1.0)
    if "Density" in n_principled.inputs:
        n_principled.inputs["Density"].default_value = 0.07
    if "Anisotropy" in n_principled.inputs:
        n_principled.inputs["Anisotropy"].default_value = 0.0
    if "Emission Strength" in n_principled.inputs:
        n_principled.inputs["Emission Strength"].default_value = 0.85

    nt.links.new(n_info.outputs["Density"], n_map.inputs["Value"])
    nt.links.new(n_map.outputs["Result"], n_ramp.inputs["Fac"])
    if "Density" in n_principled.inputs:
        nt.links.new(n_map.outputs["Result"], n_principled.inputs["Density"])
    if "Emission Color" in n_principled.inputs:
        nt.links.new(n_ramp.outputs["Color"], n_principled.inputs["Emission Color"])
    if "Emission Strength" in n_principled.inputs:
        nt.links.new(n_map.outputs["Result"], n_principled.inputs["Emission Strength"])
    nt.links.new(n_principled.outputs["Volume"], n_out.inputs["Volume"])

    # DUST MATERIAL (absorption)
    mat_dust = bpy.data.materials.new(MAT_DUST_NAME)
    mat_dust.use_nodes = True
    nt2 = mat_dust.node_tree
    nt2.nodes.clear()

    n2_out = nt2.nodes.new("ShaderNodeOutputMaterial")
    n2_info = nt2.nodes.new("ShaderNodeVolumeInfo")
    n2_map = nt2.nodes.new("ShaderNodeMapRange")
    n2_abs = nt2.nodes.new("ShaderNodeVolumeAbsorption")

    n2_info.location = (-500, 0)
    n2_map.location = (-260, 0)
    n2_abs.location = (-20, 0)
    n2_out.location = (180, 0)

    n2_map.inputs["From Min"].default_value = 0.0
    n2_map.inputs["From Max"].default_value = 1.8
    n2_map.inputs["To Min"].default_value = 0.0
    n2_map.inputs["To Max"].default_value = 2.2
    n2_map.clamp = True

    n2_abs.inputs["Color"].default_value = (0.24, 0.14, 0.08, 1.0)

    nt2.links.new(n2_info.outputs["Density"], n2_map.inputs["Value"])
    nt2.links.new(n2_map.outputs["Result"], n2_abs.inputs["Density"])
    nt2.links.new(n2_abs.outputs["Volume"], n2_out.inputs["Volume"])

    # HAZE MATERIAL: emissive nebula from density + color grids + noise
    mat_haze = bpy.data.materials.new(MAT_HAZE_NAME)
    mat_haze.use_nodes = True
    nt3 = mat_haze.node_tree
    nt3.nodes.clear()

    n3_out = nt3.nodes.new("ShaderNodeOutputMaterial")
    n3_info = nt3.nodes.new("ShaderNodeVolumeInfo")
    n3_map = nt3.nodes.new("ShaderNodeMapRange")
    n3_temp_map = nt3.nodes.new("ShaderNodeMapRange")
    n3_temp_ramp = nt3.nodes.new("ShaderNodeValToRGB")
    n3_principled = nt3.nodes.new("ShaderNodeVolumePrincipled")
    n3_texcoord = nt3.nodes.new("ShaderNodeTexCoord")
    n3_noise = nt3.nodes.new("ShaderNodeTexNoise")
    n3_noise_map = nt3.nodes.new("ShaderNodeMapRange")
    emit_mult = nt3.nodes.new("ShaderNodeMath")
    emit_final = nt3.nodes.new("ShaderNodeMath")

    n3_info.location = (-800, 0)
    n3_map.location = (-560, 0)
    n3_temp_map.location = (-560, -180)
    n3_temp_ramp.location = (-300, -180)
    n3_principled.location = (160, 40)
    n3_out.location = (380, 40)
    n3_texcoord.location = (-1040, -420)
    n3_noise.location = (-800, -420)
    n3_noise_map.location = (-560, -420)
    emit_mult.location = (0, -220)
    emit_final.location = (180, -220)

    # Noise settings
    # Noise settings
    n3_noise.noise_dimensions = '3D'
    n3_noise.inputs["Scale"].default_value = 5.0
    n3_noise.inputs["Detail"].default_value = 3.0
    n3_noise.inputs["Roughness"].default_value = 0.45

    # Noise remap
    n3_noise_map.inputs["From Min"].default_value = 0.18
    n3_noise_map.inputs["From Max"].default_value = 0.82
    n3_noise_map.inputs["To Min"].default_value = 0.18
    n3_noise_map.inputs["To Max"].default_value = 0.72
    n3_noise_map.clamp = True

    # Emission math: emit_mult = noise * gain, emit_final = density * emit_mult
    emit_mult.operation = 'MULTIPLY'
    emit_mult.inputs[0].default_value = 1.7  # gain

    emit_final.operation = 'MULTIPLY'

    # Density remap
    n3_map.inputs["From Min"].default_value = 0.0
    n3_map.inputs["From Max"].default_value = 0.42
    n3_map.inputs["To Min"].default_value = 0.0
    n3_map.inputs["To Max"].default_value = 0.46
    n3_map.clamp = True

    # Temperature remap: normalized temp 0..1 -> palette factor
    n3_temp_map.inputs["From Min"].default_value = 0.0
    n3_temp_map.inputs["From Max"].default_value = 1.0
    n3_temp_map.inputs["To Min"].default_value = 0.0
    n3_temp_map.inputs["To Max"].default_value = 1.0
    n3_temp_map.clamp = True

    r = n3_temp_ramp.color_ramp
    while len(r.elements) > 2:
        r.elements.remove(r.elements[-1])

    # outer edges: faint pink-magenta
    r.elements[0].position = 0.0
    r.elements[0].color = (0.94, 0.50, 0.78, 1.0)

    # hot core: bluish white
    r.elements[1].position = 1.0
    r.elements[1].color = (0.86, 0.92, 1.00, 1.0)

    # mid purple/blue transitions
    e1 = r.elements.new(0.32)
    e1.color = (0.68, 0.46, 0.94, 1.0)
    e2 = r.elements.new(0.66)
    e2.color = (0.42, 0.56, 0.98, 1.0)

    if "Color" in n3_principled.inputs:
        n3_principled.inputs["Color"].default_value = (0.42, 0.50, 0.72, 1.0)
    if "Density" in n3_principled.inputs:
        n3_principled.inputs["Density"].default_value = 0.12
    if "Anisotropy" in n3_principled.inputs:
        n3_principled.inputs["Anisotropy"].default_value = 0.18
    if "Emission Color" in n3_principled.inputs:
        n3_principled.inputs["Emission Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    if "Emission Strength" in n3_principled.inputs:
        n3_principled.inputs["Emission Strength"].default_value = 1.0

    # Haze wiring
    nt3.links.new(n3_texcoord.outputs["Object"], n3_noise.inputs["Vector"])
    nt3.links.new(n3_noise.outputs["Fac"], n3_noise_map.inputs["Value"])

    # Density from VDB
    nt3.links.new(n3_info.outputs["Density"], n3_map.inputs["Value"])

    # Temperature from VDB -> palette ramp
    nt3.links.new(n3_info.outputs["Temperature"], n3_temp_map.inputs["Value"])
    nt3.links.new(n3_temp_map.outputs["Result"], n3_temp_ramp.inputs["Fac"])

    # Density into Principled Volume
    if "Density" in n3_principled.inputs:
        nt3.links.new(n3_map.outputs["Result"], n3_principled.inputs["Density"])

    # Palette color into emission color
    if "Emission Color" in n3_principled.inputs:
        nt3.links.new(n3_temp_ramp.outputs["Color"], n3_principled.inputs["Emission Color"])

    # Emission strength = density mask * noisy hotspot mask * gain
    nt3.links.new(n3_noise_map.outputs["Result"], emit_mult.inputs[1])
    nt3.links.new(n3_map.outputs["Result"], emit_final.inputs[0])
    nt3.links.new(emit_mult.outputs[0], emit_final.inputs[1])

    if "Emission Strength" in n3_principled.inputs:
        nt3.links.new(emit_final.outputs[0], n3_principled.inputs["Emission Strength"])

    nt3.links.new(n3_principled.outputs["Volume"], n3_out.inputs["Volume"])

    # Assign materials
    if ion_obj:
        ion_obj.hide_render = True
    if dust_obj:
        dust_obj.data.materials.clear()
        dust_obj.data.materials.append(mat_dust)
    if haze_obj:
        haze_obj.data.materials.clear()
        haze_obj.data.materials.append(mat_haze)


def setup_camera():
    cam_col = ensure_collection(CAM_COLLECTION_NAME, parent=ensure_collection(NEBULA_COLLECTION_NAME))

    remove_object_if_exists(CAMERA_NAME)
    cam_data = bpy.data.cameras.get(CAMERA_NAME)
    if cam_data is not None:
        bpy.data.cameras.remove(cam_data, do_unlink=True)

    cam_data = bpy.data.cameras.new(CAMERA_NAME)
    cam_obj = bpy.data.objects.new(CAMERA_NAME, cam_data)
    cam_col.objects.link(cam_obj)

    cam_data.lens = 72.0
    cam_data.sensor_width = 36.0
    cam_data.clip_start = 0.01
    cam_data.clip_end = 5000.0
    cam_data.dof.use_dof = False

    cam_obj.location = (0.4, -10.8, 1.35)
    cam_obj.rotation_euler = (math.radians(88.0), 0.0, math.radians(32.0))

    bpy.context.scene.camera = cam_obj
    return cam_obj

def create_star_field():
    light_col = ensure_collection(LIGHT_COLLECTION_NAME, parent=ensure_collection(NEBULA_COLLECTION_NAME))

    # remove old stars
    for obj in list(light_col.objects):
        if obj.name.startswith("NB_Star_"):
            bpy.data.objects.remove(obj, do_unlink=True)

    star_count = 1200
    spread_x = 28.0
    spread_z = 28.0

    for i in range(star_count):
        # ultra‑small sphere, will render as a dot with emission + glare
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=0.0016 + random.random() * 0.0024,
            location=(
                random.uniform(-spread_x, spread_x),
                random.uniform(-2.5, 6.0),
                random.uniform(-spread_z, spread_z),
            ),
        )
        star = bpy.context.active_object
        star.name = f"NB_Star_{i:04d}"

        # keep stars out of the nebula core silhouette most of the time
        if abs(star.location.x) < 2.0 and abs(star.location.z) < 2.0 and random.random() < 0.9:
            star.location.x += math.copysign(
                3.8 + random.random() * 4.5,
                star.location.x if star.location.x != 0 else random.choice([-1, 1]),
            )

        for c in list(star.users_collection):
            c.objects.unlink(star)
        light_col.objects.link(star)

        mat_name = f"NB_Star_Mat_{i:04d}"
        mat = bpy.data.materials.new(mat_name)
        mat.use_nodes = True
        nt = mat.node_tree
        nt.nodes.clear()

        n_out = nt.nodes.new("ShaderNodeOutputMaterial")
        n_em = nt.nodes.new("ShaderNodeEmission")

        n_em.location = (0, 0)
        n_out.location = (220, 0)

        # temperature-ish buckets → color
        t = random.random()
        if t < 0.10:
            # richer red/orange
            col = (1.0, 0.64, 0.38, 1.0)
            strength = 3.0 + random.random() * 7.5
        elif t < 0.25:
            # warmer golden stars
            col = (1.0, 0.78, 0.50, 1.0)
            strength = 3.5 + random.random() * 8.0
        elif t < 0.65:
            # neutral stars still slightly warm
            col = (1.0, 0.95, 0.88, 1.0)
            strength = 2.4 + random.random() * 6.0
        elif t < 0.90:
            # cleaner icy blue-white
            col = (0.78, 0.90, 1.0, 1.0)
            strength = 3.2 + random.random() * 7.5
        else:
            # vivid hot blue
            col = (0.56, 0.76, 1.0, 1.0)
            strength = 5.0 + random.random() * 10.0

        # a few brighter "anchor" stars, but still tiny
        if random.random() < 0.018:
            star.scale *= 1.7
            strength *= 2.8

        n_em.inputs["Color"].default_value = col
        n_em.inputs["Strength"].default_value = strength
        nt.links.new(n_em.outputs["Emission"], n_out.inputs["Surface"])

        star.data.materials.clear()
        star.data.materials.append(mat)

def create_central_star():
            light_col = ensure_collection(LIGHT_COLLECTION_NAME, parent=ensure_collection(NEBULA_COLLECTION_NAME))

            old = bpy.data.objects.get("NB_CentralStar")
            if old is not None:
                bpy.data.objects.remove(old, do_unlink=True)

            # very small core → relies on emission + glare to feel bright
            bpy.ops.mesh.primitive_uv_sphere_add(radius=0.009, location=(0.0, 0.0, 0.02))
            star = bpy.context.active_object
            star.name = "NB_CentralStar"

            for c in list(star.users_collection):
                c.objects.unlink(star)
            light_col.objects.link(star)

            mat = bpy.data.materials.new("NB_CentralStar_Mat")
            mat.use_nodes = True
            nt = mat.node_tree
            nt.nodes.clear()

            n_out = nt.nodes.new("ShaderNodeOutputMaterial")
            n_em = nt.nodes.new("ShaderNodeEmission")

            n_em.location = (0, 0)
            n_out.location = (220, 0)

            # hot, slightly warm-white core
            n_em.inputs["Color"].default_value = (0.86, 0.90, 1.0, 1.0)
            n_em.inputs["Strength"].default_value = 52.0

            nt.links.new(n_em.outputs["Emission"], n_out.inputs["Surface"])

            star.data.materials.clear()
            star.data.materials.append(mat)

def setup_compositor():
    scene = bpy.context.scene

    try:
        scene.use_nodes = True
    except Exception:
        log("Compositor nodes unavailable in this environment; skipping compositor setup.")
        return

    nt = getattr(scene, "node_tree", None)
    if nt is None:
        log("Scene has no node_tree; skipping compositor setup.")
        return

    nt.nodes.clear()

    n_rl = nt.nodes.new("CompositorNodeRLayers")
    n_denoise = nt.nodes.new("CompositorNodeDenoise")
    n_glare = nt.nodes.new("CompositorNodeGlare")
    n_cb = nt.nodes.new("CompositorNodeColorBalance")
    n_curves = nt.nodes.new("CompositorNodeRGBCurves")
    n_comp = nt.nodes.new("CompositorNodeComposite")

    n_rl.location = (-700, 0)
    n_denoise.location = (-460, 0)
    n_glare.location = (-200, 80)
    n_cb.location = (60, 60)
    n_curves.location = (320, 40)
    n_comp.location = (560, 40)

    n_glare.glare_type = 'FOG_GLOW'
    n_glare.quality = 'HIGH'
    n_glare.mix = 0.36
    n_glare.threshold = 0.92
    n_glare.size = 6

    if hasattr(n_cb, "correction_method"):
        n_cb.correction_method = 'LIFT_GAMMA_GAIN'
    n_cb.lift = (0.99, 0.99, 1.01)
    n_cb.gamma = (1.02, 1.00, 1.03)
    n_cb.gain = (0.96, 0.94, 1.00)

    curve = n_curves.mapping.curves[3]
    curve.points.new(0.10, 0.06)
    curve.points.new(0.32, 0.38)
    curve.points.new(0.78, 0.86)
    n_curves.mapping.update()

    nt.links.new(n_rl.outputs["Image"], n_denoise.inputs["Image"])
    if "Denoising Normal" in n_rl.outputs and "Normal" in n_denoise.inputs:
        nt.links.new(n_rl.outputs["Denoising Normal"], n_denoise.inputs["Normal"])
    if "Denoising Albedo" in n_rl.outputs and "Albedo" in n_denoise.inputs:
        nt.links.new(n_rl.outputs["Denoising Albedo"], n_denoise.inputs["Albedo"])
    nt.links.new(n_denoise.outputs["Image"], n_glare.inputs["Image"])
    nt.links.new(n_glare.outputs["Image"], n_cb.inputs["Image"])
    nt.links.new(n_cb.outputs["Image"], n_curves.inputs["Image"])
    nt.links.new(n_curves.outputs["Image"], n_comp.inputs["Image"])


def apply_stage_profile(stage):
    scene = bpy.context.scene
    c = scene.cycles

    if stage == STAGE_PREVIEW:
        scene.render.resolution_percentage = 50
        c.samples = 64
        if hasattr(c, "volume_step_rate"):
            c.volume_step_rate = 1.5
        if hasattr(c, "volume_max_steps"):
            c.volume_max_steps = 128
        c.max_bounces = 8
    elif stage == STAGE_LOOKDEV:
        scene.render.resolution_percentage = 75
        c.samples = 256
        if hasattr(c, "volume_step_rate"):
            c.volume_step_rate = 0.8
        if hasattr(c, "volume_max_steps"):
            c.volume_max_steps = 256
        c.max_bounces = 12
    elif stage == STAGE_FINAL:
        scene.render.resolution_percentage = 100
        c.samples = 1024
        if hasattr(c, "volume_step_rate"):
            c.volume_step_rate = 0.3
        if hasattr(c, "volume_max_steps"):
            c.volume_max_steps = 1024
        c.max_bounces = 20


def main():
    scene_bootstrap()
    cache_dir = ensure_cache_dir()

    src_obj = build_source_pointclouds()
    generate_butterfly_fields(src_obj, num_points=30000)

    # Unique filenames per run to avoid Blender VDB cache issues
    run_tag = str(int(random.random() * 1_000_000))
    ion_path = os.path.join(cache_dir, f"ion_gas_{run_tag}.vdb")
    dust_path = os.path.join(cache_dir, f"dust_{run_tag}.vdb")
    haze_path = os.path.join(cache_dir, f"haze_{run_tag}.vdb")

    wrote = write_vdb_files(src_obj, ion_path, dust_path, haze_path, voxel_size=0.05)

    ion_obj, dust_obj, haze_obj = import_volumes(ion_path, dust_path, haze_path)
    build_materials(ion_obj, dust_obj, haze_obj)
    setup_camera()
    create_star_field()
    create_central_star()
    setup_compositor()
    apply_stage_profile(STAGE_FINAL)

    if not wrote:
        log("No VDBs were written because openvdb binding is missing or incomplete.")
        log("Blender-side scene setup is complete.")
        log(f"Write these files externally, then rerun:\n  {ion_path}\n  {dust_path}\n  {haze_path}")
    else:
        log("Pipeline complete; ready to render.")


if __name__ == "__main__":
    main()