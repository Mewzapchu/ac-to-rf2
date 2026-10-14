"""Geometry stage: AC kn5 -> Blender -> rFactor 2 .gmt

Run headless:
  blender --background --python blender_stage.py -- <material_map.json> <out_dir> [--limit N]

Reads the mapping produced by convert_track.py, rebuilds every mesh in Blender
with its rF2 material name, and exports one gmt per mesh using Traveller's
io_rf2_gmt addon. Writes exported.json listing the gmt files actually produced,
so the scene file can be rebuilt from reality rather than from guesses.
"""
import os
import sys
import json

import bpy
import addon_utils
from mathutils import Matrix, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from kn5 import KN5  # noqa: E402
import backdrop  # noqa: E402

ADDON = "io_rf2_gmt_release"


def argv():
    a = sys.argv
    return a[a.index("--") + 1:] if "--" in a else []


def world_matrix(node):
    """kn5 stores row major matrices with translation in the last row."""
    chain = []
    n = node
    while n is not None:
        if n.matrix:
            rows = [n.matrix[i:i + 4] for i in range(0, 16, 4)]
            chain.append(Matrix(rows).transposed())
        n = n.parent
    m = Matrix.Identity(4)
    for mat in reversed(chain):
        m = m @ mat
    return m


# Measured, not assumed: the exporter writes a Blender vertex (x, y, z) into
# the gmt as (x, z, y), swapping Y and Z with no negation. AC and rF2 share the
# same up axis, so we pre-swap here and the exporter's swap cancels it out,
# leaving AC world coordinates in the file.
YZ_SWAP = Matrix(((1, 0, 0, 0),
                  (0, 0, 1, 0),
                  (0, 1, 0, 0),
                  (0, 0, 0, 1)))

# AC and rF2 share an up axis but not a handedness, so coordinates copied
# straight across come out mirrored: every left hander becomes a right hander.
# One negation fixes it, and the same negation is applied to the waypoints,
# spawn points and cameras so the whole track stays consistent.
MIRROR = Matrix(((-1, 0, 0, 0),
                 (0, 1, 0, 0),
                 (0, 0, 1, 0),
                 (0, 0, 0, 1)))
AC_TO_RF2 = YZ_SWAP @ MIRROR


def make_template():
    """Let the addon build its scene, then reuse its object as a stencil.

    The exporter reads custom properties the addon writes onto the mesh data
    (flag bits, Normals, SourceMesh) and onto the material. Meshes created from
    scratch have none of those, so every object is cloned from this one.
    """
    bpy.ops.mesh.primitive_cube_add()
    try:
        bpy.ops.mesh.add_rf2_object()
    except Exception as exc:
        print("!! add_rf2_object: %s" % exc)
    obs = [o for o in bpy.data.objects
           if o.type == "MESH" and o.data.materials and o.data.materials[0]]
    if not obs:
        raise RuntimeError("addon did not create a template object with a material")
    ob = obs[-1]
    empty = ob.parent
    if empty is None or "File_Name" not in empty.keys():
        raise RuntimeError("template mesh has no rF2 empty parent")
    # Must come off the object, not bpy.data.materials[0], which is Blender's
    # default grease pencil material and carries none of the rF2 properties.
    mat = ob.data.materials[0]
    coll = bpy.data.collections.get("GMTs") or bpy.context.collection
    print("template empty %s, mesh %s, material %s, collection %s"
          % (empty.name, ob.name, mat.name, coll.name))
    return empty, ob, mat, coll


# rF2's own shaders, and every texture stage each one declares. They are all
# required: leave one empty and the exporter writes the slot's own name where
# the filename belongs, so rF2 looks for a texture called "sBump" and fails
# the entire scene rather than that one material.
#
# TWO_STAGE samples a base and a detail through separate UV layers, sBase on
# UV_A and sBase1 on UV_B, which is the one thing a single baked texture can
# never reproduce: the detail keeps its own resolution however far the base is
# stretched. BUMP is the same minus the second diffuse.
# Where to aim an ordinary surface's albedo, from the 342 textures measured
# across Joesville and Loch Drummond: their albedo runs p25 51, median 78,
# p75 115. 70 sits just under the middle of that, which pulls the few AC
# textures that are far too bright into the band and leaves everything
# already darker than it untouched.
SURFACE_ALBEDO = 70.0
# Cut-out foliage aims a little lower, at their own grass: Grass_Natural is
# 64.8 and their tree foliage 55.
CUTOUT_ALBEDO = 65.0
# The road's lift in bake_layers, so the vegetation keeps its ratio to it.
ROAD_LIFT = 157.3 / 143.0
# The calibration point, as in bake_layers: Laguna's road as AC with CSP
# lights it, ksDiffuse 0.42 / ksAmbient 0.35, which rF2 draws at 1.0 on a
# texture lifted by ROAD_LIFT. Every material is scaled from AC's
# coefficients by this pair.
# The scene's far clip plane (finalize_track's mainview ClipPlanes).
DRAW_RANGE = 13000.0
GRASS_CARD_DIM = 0.25
GRASS_GROUND_DIM = 0.30
ANCHOR_KD = 0.42
ANCHOR_KA = 0.35

TWO_STAGE_SHADER = "L2BUMPSPECMAPT0XT1"
TWO_STAGE_SLOTS = ("sBase", "sBase1", "sSpec", "sBump")
BUMP_SHADER = "L2BUMPSPECMAPT0"
BUMP_SLOTS = ("sBase", "sSpec", "sBump")

# How far a decal is lifted off the surface it lies on. A centimetre is below
# anything you can see and far above the depth buffer's precision at the range
# these are viewed from.
DECAL_LIFT = 0.01

# Stand-ins for the stages AC gives nothing for, written by build_track.py.
# Both are the neutral value for how rF2 uses that stage.
FLAT_SPEC = "_flat_spec.dds"
FLAT_NM = "_flat_nm.dds"
WHITE_TEX = "_white.dds"     # for materials AC leaves untextured


# Which name actually moves each bit, measured with probe_colorkey.py by
# setting every candidate on its own mesh and reading the exported byte back.
# The addon is not consistent about this: Flag_0x00000080 works for colour
# keying but Flag_0x00000010 does nothing for filtering, where only
# LinearFilter does. Every other spelling accepts the assignment and is
# silently ignored, as the blend factors and Alpha_Reference were, so these
# are the measured names and verify_install checks the bytes.
TEX_FLAG_NAMES = {
    0x80: ("ColorKey", "Flag_0x00000080"),
    0x10: ("LinearFilter",),
}


def set_texture_flag(node, bit):
    """Switch a texture flag on, by whichever name the addon reads."""
    for name in TEX_FLAG_NAMES.get(bit, ()):
        try:
            setattr(node, name, True)
            return True
        except Exception:
            continue
    return False


def make_shader_template(template_ob, shader, slots):
    """Build one material carrying a shader, to copy per material later."""
    import importlib
    nodes_mod = importlib.import_module("io_rf2_gmt_release.rF2_Nodes_Stuff")
    ob = template_ob.copy()
    ob.data = template_ob.data.copy()
    bpy.context.collection.objects.link(ob)
    mat = ob.data.materials[0].copy()
    ob.data.materials.clear()
    ob.data.materials.append(mat)
    try:
        # Takes the group as one of its plain strings, and the object rather
        # than its mesh data.
        mat, ob = nodes_mod.CreateNodeTree(mat, ob, "BUMP", shader)
    except Exception as exc:
        print("!! %s template: %s" % (shader, exc))
        bpy.data.objects.remove(ob, do_unlink=True)
        return None
    missing = [n for n in slots if mat.node_tree.nodes.get(n) is None]
    bpy.data.objects.remove(ob, do_unlink=True)
    if missing:
        print("!! %s template is missing %s" % (shader, missing))
        return None
    print("%s template ready with %s" % (shader, list(slots)))
    return mat


def sanitize(name):
    out = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    return out or "mesh"


# A gmt addresses corners with a 16 bit index, so a mesh has to stay under
# 65535 of them. Anything past ~21845 triangles takes the exporter down with an
# access violation rather than an error, so split before handing it over.
MAX_TRIS = 20000


def split_node(node):
    """Yield (verts, uvs, faces, normals) chunks with vertices remapped per chunk.

    normals are AC's own, per vertex, or None where the kn5 had none.
    """
    idx = node.indices
    ntri = node.index_count // 3
    src_n = node.normals if node.normals and len(node.normals) == len(node.verts) else None
    for start in range(0, ntri, MAX_TRIS):
        remap = {}
        verts, uvs, faces, normals = [], [], [], []
        for t in range(start, min(start + MAX_TRIS, ntri)):
            face = []
            for vi in idx[t * 3:t * 3 + 3]:
                local = remap.get(vi)
                if local is None:
                    local = remap[vi] = len(verts)
                    verts.append(tuple(node.verts[vi]))
                    uvs.append(node.uvs[vi])
                    if src_n is not None:
                        normals.append(src_n[vi])
                face.append(local)
            faces.append(tuple(face))
        yield verts, uvs, faces, (normals if src_n is not None else None)


def lift_off_surface(verts, faces, amount):
    """Nudge a decal along its own normal, away from what it is lying on.

    AC's rubber and its sand trap borders are coplanar with the road and the
    terrain: same plane, same depth, so the two fight over every pixel and the
    surface flickers as the camera moves. A centimetre is invisible and settles
    it. Per vertex, from the faces around it, so it follows a cambered road
    rather than assuming the world is flat.
    """
    acc = [[0.0, 0.0, 0.0] for _ in verts]
    for a, b, c in faces:
        pa, pb, pc = verts[a], verts[b], verts[c]
        u = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
        v = (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2])
        n = (u[1] * v[2] - u[2] * v[1],
             u[2] * v[0] - u[0] * v[2],
             u[0] * v[1] - u[1] * v[0])
        for i in (a, b, c):
            acc[i][0] += n[0]
            acc[i][1] += n[1]
            acc[i][2] += n[2]
    out = []
    for p, n in zip(verts, acc):
        length = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) ** 0.5
        if length < 1e-12:
            out.append(p)
            continue
        k = amount / length
        out.append((p[0] + n[0] * k, p[1] + n[1] * k, p[2] + n[2] * k))
    return out


def build_mesh(geom, matrix, name, material, template_empty, template_ob, coll,
               uv_repeat=1.0, uv_b=None, uv_d=None, lift=0.0):
    verts, uvs, faces = geom[:3]
    normals = geom[3] if len(geom) > 3 else None
    # Bake the placement into the vertices and keep every object transform at
    # identity. Left on the object, the rotation is stored in the gmt and
    # applied again at runtime, which stands the whole track on its side.
    verts = [tuple(matrix @ Vector(v)) for v in verts]
    if normals:
        # Normals go through the inverse transpose, which for the rotations
        # and the mirror here is the rotation itself, and stays right if a
        # node is ever scaled unevenly.
        nmat = matrix.to_3x3().inverted_safe().transposed()
        normals = [tuple((nmat @ Vector(n)).normalized()) for n in normals]
    # A Y/Z swap is a mirror. Mirroring the positions while leaving the vertex
    # order alone turns every face inside out, so the road ends up facing down:
    # invisible from above and impossible to stand on. Reverse the winding to
    # put the surfaces back the right way round.
    if matrix.determinant() < 0:
        faces = [tuple(reversed(f)) for f in faces]
    if lift:
        verts = lift_off_surface(verts, faces, lift)
    me = template_ob.data.copy()
    me.clear_geometry()
    me.from_pydata(verts, [], faces)
    me.validate(verbose=False)
    # AC's own vertex normals, not ones rebuilt from the winding. Every grass
    # billboard on Silverstone has its normals pointing straight up so the
    # cards light like the ground under them; rebuilt from the winding they
    # pointed sideways, 90 degrees off on every one of 1.38 million
    # triangles, so each card was lit as a little wall -- bright facing the
    # sun, dark facing away. Crowds are the same, and half the tree
    # triangles are more than 60 degrees off. The exporter writes split
    # normals (measured with probe_normals.py), which is where these live.
    if normals and len(normals) == len(me.vertices):
        me.use_auto_smooth = True
        me.normals_split_custom_set_from_vertices(normals)
    # clear_geometry() drops every layer, and the exporter reads all of these
    # by name. Recreate them or it fails on the first polygon. These go in with
    # foreach_set: per element Python writes across eight layers are slow
    # enough on a million triangle mesh to take Blender down.
    nloops = len(me.loops)
    lv = [0] * nloops
    me.loops.foreach_get("vertex_index", lv)
    uvflat = [0.0] * (nloops * 2)
    for i, vi in enumerate(lv):
        u, v = uvs[vi]
        uvflat[i * 2] = u * uv_repeat
        uvflat[i * 2 + 1] = (1.0 - v) * uv_repeat
    # Each texture stage reads its own UV layer: sBase on UV_A, sBase1 on
    # UV_B, sSpec on UV_C and sBump on UV_D. A two stage material tiles its
    # detail on UV_B while the base keeps its own mapping, and takes UV_D with
    # it when the normal map is the detail's rather than the base's. The two
    # axes scale separately: a road's mapping is far longer along the track
    # than across it, and one factor for both smears the grain.
    scaled = {}
    for uv_name, factor in (("UV_B", uv_b), ("UV_D", uv_d)):
        if not factor:
            continue
        fu, fv = factor
        layer = list(uvflat)
        layer[0::2] = [c * fu for c in uvflat[0::2]]
        layer[1::2] = [c * fv for c in uvflat[1::2]]
        scaled[uv_name] = layer
    for uv_name in ("UV_A", "UV_B", "UV_C", "UV_D"):
        me.uv_layers.new(name=uv_name).data.foreach_set(
            "uv", scaled.get(uv_name, uvflat))
    white = [1.0] * (nloops * 4)
    for col_name in ("Diffuse Color", "Diffuse Alpha",
                     "Specular Color", "Specular Alpha"):
        layer = me.color_attributes.new(name=col_name, domain="CORNER",
                                        type="BYTE_COLOR")
        layer.data.foreach_set("color", white)
    # Which normal the exporter writes, read from its own code (process_polygon
    # in rF2_GMT_Export.pyc) and checked byte for byte with probe_normals.py:
    #   0 -> Blender's split normals, so AC's own normals set above
    #   1 -> the face normal, with its Y and Z left unswapped: a floor comes
    #        out facing sideways
    #   anything else -> the constant (0, 0, 1), straight up, for every vertex
    # We were writing 2. Every wall, ceiling, post and truck side on every
    # track was lit as though it faced the sky: a stand's ceiling took full
    # sun, a wall facing away from the sun was as bright as one facing it,
    # and no upright surface had any shading at all -- the walls, seats and
    # white trucks that kept coming out too bright however their material
    # was turned down. 0 writes each vertex's real normal.
    me["Normals"] = 0
    me.materials.clear()
    me.materials.append(material)
    me.name = name + "_Mesh"

    # One gmt per empty. The empty carries File_Name, which is what the
    # exporter writes the file as, and the mesh hangs underneath it.
    empty = template_empty.copy()
    empty.name = name
    empty["File_Name"] = name
    coll.objects.link(empty)
    empty.matrix_world = Matrix.Identity(4)
    empty.rFactor = "2B"

    ob = template_ob.copy()
    ob.data = me
    ob.name = name + "_Mesh"
    coll.objects.link(ob)
    ob.parent = empty
    ob.matrix_parent_inverse = Matrix.Identity(4)
    ob.matrix_local = Matrix.Identity(4)
    ob.rFactor = "2B"
    # The exporter writes a visible range into every gmt, 1000 m unless told
    # otherwise, and rF2 stops drawing a mesh whose centre is further away
    # than that. AC draws these with no limit. At Spa that culled the far
    # trees and terrain in and out as you drove, and the two rubber meshes,
    # 1.35 km in radius, vanished whenever their centre was a kilometre off
    # even with the car on top of them. Out to the scene's far clip, plus the
    # mesh's own size, so nothing goes before the horizon does.
    reach = DRAW_RANGE + mesh_radius(verts)
    for o in (empty, ob):
        try:
            o.LOD_In, o.LOD_Out = 0.0, reach
        except Exception:
            o["LOD_In"], o["LOD_Out"] = 0.0, reach
    return empty, ob


def mesh_radius(verts):
    """Half the diagonal of the vertices' bounding box."""
    if not verts:
        return 0.0
    lo = [min(v[i] for v in verts) for i in range(3)]
    hi = [max(v[i] for v in verts) for i in range(3)]
    return 0.5 * sum((hi[i] - lo[i]) ** 2 for i in range(3)) ** 0.5


def silence_popups():
    """Turn the exporter's popup messages into printed lines.

    When a mesh has a geometry issue the add-on reports it with
    WindowManager.popup_menu. We run Blender in the background, where there is
    no window to put a popup in, and Blender dies inside UI_popup_menu_end with
    an access violation: Spa crashed that way at mesh 1151 of 1728, an hour
    in. Any track with one such mesh would. The popup's contents are captured
    by running its draw function against a stand-in layout and printed, so the
    issue is still on record.
    """
    class _Layout:
        def __init__(self, lines):
            self._lines = lines

        def label(self, text="", **_kw):
            if text:
                self._lines.append(text)
            return self

        def __getattr__(self, _name):
            return lambda *a, **k: self

    class _Self:
        def __init__(self, lines):
            self.layout = _Layout(lines)

    def popup_menu(self, draw_func=None, title="", icon="NONE"):
        lines = []
        try:
            if draw_func is not None:
                draw_func(_Self(lines), bpy.context)
        except Exception:
            pass
        print("!! exporter warning (popup suppressed): %s %s"
              % (title, " | ".join(lines)[:400]))
        sys.stdout.flush()

    bpy.types.WindowManager.popup_menu = popup_menu


def main():
    args = argv()
    # Absolute throughout: Blender resolves relative paths against the blend
    # file, not the working directory, so image loads silently miss otherwise.
    map_path = os.path.abspath(args[0])
    out_dir = os.path.abspath(args[1])
    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    # AC bakes lighting into its textures, so surfaces read brighter than cars
    # rF2 lights itself. These scale every material down to compensate.
    # Fallbacks for materials that declare no coefficients, plus a global trim.
    diffuse_level = float(args[args.index("--diffuse") + 1]) if "--diffuse" in args else 0.42
    ambient_level = float(args[args.index("--ambient") + 1]) if "--ambient" in args else 0.30
    # A 30 degree sun, which is what AC asks for, gives flat ground sin30/sin72
    # of the light our old overhead one did. rF2's light is already near its
    # ceiling at 245 of 255, so the shortfall is made up here instead, on
    # coefficients that sit at 0.38 with room to 1.0.
    # A trim, not a correction. The translation is right without it now.
    diffuse_scale = float(args[args.index("--gain") + 1]) if "--gain" in args else 1.0
    ambient_scale = diffuse_scale
    os.makedirs(out_dir, exist_ok=True)

    addon_utils.enable(ADDON, default_set=True, persistent=True)
    silence_popups()
    data = json.load(open(map_path))
    by_mesh = {m["mesh"]: m for m in data["meshes"]}

    template_empty, template_ob, template_mat, coll = make_template()

    tex_dir = os.path.join(os.path.dirname(map_path), "textures")
    tex_files = data.get("texture_files", {})
    images = {}

    def image_for(filename):
        if filename in images:
            return images[filename]
        # convert_track.py sanitizes texture names on the way out, so
        # "tyres-protections.dds" is on disk as "tyres_protections.dds".
        # Look it up under the same name it was written as.
        on_disk = tex_files.get(filename)
        if on_disk is None:
            base, ext = os.path.splitext(filename)
            on_disk = sanitize(base) + ext
        path = os.path.join(tex_dir, on_disk)
        img = None
        if os.path.exists(path):
            try:
                img = bpy.data.images.load(path, check_existing=True)
                img.name = on_disk
            except Exception as exc:
                print("!! image %s: %s" % (on_disk, exc))
        else:
            print("!! texture missing on disk: %s (from %s)" % (on_disk, filename))
        images[filename] = img
        return img

    # The middle of AC's own range for this track, which is what gets mapped
    # onto rF2's 1.0. Taken over the materials that are actually drawn, so a
    # pile of invisible physics shells cannot drag it.
    import statistics as _st
    _seen, _kd, _ka = set(), [], []
    for _row in data["meshes"]:
        if _row["rf2_material"] in _seen or not _row.get("render", True):
            continue
        _seen.add(_row["rf2_material"])
        _p = _row.get("props", {})
        _kd.append(float(_p.get("ksDiffuse", 0.0)) or diffuse_level)
        _ka.append(float(_p.get("ksAmbient", 0.0)) or ambient_level)
    # The upper quartile rather than the median. Mapping the median onto 1.0
    # pushes everything above it into the ceiling, where it all flattens
    # together: tree bark at 0.30 and asphalt at 0.38 both arrive as 1.0 and
    # the bark loses the darkness AC gave it. The upper quartile leaves most
    # materials their relationship and saturates only the brightest few.
    def _upper(vals, fallback):
        if not vals:
            return fallback
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(round(0.75 * (len(vals) - 1))))]
    mid_diffuse = _upper(_kd, diffuse_level)
    mid_ambient = _upper(_ka, ambient_level)
    print("AC's upper quartile here: ksDiffuse %.3f, ksAmbient %.3f "
          "-- these become rF2's 1.0" % (mid_diffuse, mid_ambient))

    two_stage_mat = make_shader_template(template_ob, TWO_STAGE_SHADER,
                                         TWO_STAGE_SLOTS)
    bump_mat = make_shader_template(template_ob, BUMP_SHADER, BUMP_SLOTS)
    two_stage_used = set()
    uv_d_of = {}
    alpha_ref = {}
    # Worked out in bake_layers from the texture that ships, not from AC's
    # ksAlphaRef. Absent means the alpha is not a cut-out mask and the
    # material draws whole.
    cutout = data.get("alpha_ref", {})
    # Measured in bake_layers on the shipping textures.
    albedo = data.get("albedo_lum", {})
    albedo_p90 = data.get("albedo_p90", {})
    albedo_rgb = data.get("albedo_rgb", {})
    spec_mask = data.get("spec_mask", {})
    cutout_tex = data.get("cutout_tex", {})
    # Over-bright materials carry their darkening in a copy of the texture
    # (bake_layers.albedo_caps); the material figures barely move rF2.
    dim_tex = data.get("dim_tex", {})
    dim_scale = data.get("dim_scale", {})
    alpha_ref_names = set(cutout)
    rubber_tex = data.get("rubber_tex", {})
    counts = {"two stage": 0, "bump": 0, "plain": 0}

    def ac_texture(row, slot):
        """An AC texture for this slot, if there is a real one behind it."""
        t = row["textures"].get(slot, "NULL")
        return None if t.lower().startswith("null") else t

    materials = {}
    textured = 0
    for row in data["meshes"]:
        name = row["rf2_material"]
        if name in materials:
            continue
        pr = row.get("props", {})
        # a baked multilayer composite replaces the near blank base
        diffuse = row.get("baked") or row["textures"].get("txDiffuse")
        # A cut-out samples its own copy, whose mips hold its threshold, and
        # the rubber its more opaque grey one.
        diffuse = cutout_tex.get(name) or dim_tex.get(name) or diffuse
        nm = ac_texture(row, "txNormal")
        # A normal map for the detail: AC's own if it ships one, otherwise the
        # one bake_layers derives from the grain for ground AC leaves flat.
        detail_nm = ((row.get("two_stage") or {}).get("detail_nm")
                     or ac_texture(row, "txDetailNM"))
        maps = ac_texture(row, "txMaps")
        two = row.get("two_stage") if two_stage_mat is not None else None

        # Pick the shader, then fill every stage it declares. A stage left
        # empty makes the exporter write the slot's own name where a filename
        # belongs, and rF2 fails the entire scene over it.
        template, slots, wanted, uv_d = template_mat, (), {}, None
        if two:
            # Base on the first stage, detail on the second; rF2 multiplies
            # them per pixel, which is where the grain comes from now. Only
            # the grain normal bake_layers derives from a blended detail tiles
            # with it. AC's own txDetailNM sits on the mesh UVs: CSP's replica
            # of Kunos' multilayer maps it "with UV1" x DetailNMMult, default
            # 1. Tiled every 2 m with the detail it packed Silverstone's and
            # Spa's asphalt bumps 20-60x too densely, and under the road's
            # specular they glittered in a band in front of the car until the
            # mips flattened them -- the "repeat" with a sharp edge. Highlands'
            # road, the one that looked right, ships a flat normal map.
            # Mapped on the mesh UVs instead, the same bumps became metre-wide
            # blobs that read as frozen water caustics under the specular.
            # Neither mapping matches AC, so AC's txDetailNM is left out and
            # the surface takes its own txNormal or a flat map -- which is
            # what Highlands' road, the one that looked right, always had.
            # Both tried 2026-10-11 and reverted: the band near the car stayed
            # with a flat map, so the bump map was not its cause. Back to the
            # detail's tiling, as signed off.
            # uv_nm: the asphalt's detail tiles at 0.5 m, its bumps stay at 2 m.
            bump, uv_d = ((detail_nm, two.get("uv_nm") or two.get("uv_b"))
                          if detail_nm else (nm, None))
            template, slots = two_stage_mat, TWO_STAGE_SLOTS
            wanted = {"sBase": two["base"], "sBase1": two["detail"],
                      "sSpec": maps or two.get("spec") or FLAT_SPEC,
                      "sBump": bump or FLAT_NM}
        elif (diffuse and bump_mat is not None
                and (row.get("alpha_test")
                     or (nm and not row.get("needs_blend")))):
            # Cut-outs have to come here even without a normal map. L2DIFFUSET0
            # has no alpha test in it, so the threshold we write is never
            # consulted and the quad draws whole -- the green squares behind
            # the trees and the solid fences. Studio 397 put every tree, shrub
            # and treeline in Joesville on a bump shader and none on the plain
            # one. Where AC gives no normal or specular map the neutral
            # stand-ins fill the slots.
            template, slots = bump_mat, BUMP_SLOTS
            wanted = {"sBase": diffuse, "sSpec": maps or FLAT_SPEC,
                      "sBump": nm or FLAT_NM}

        got = {slot: image_for(wanted[slot]) for slot in slots}
        if slots and any(v is None for v in got.values()):
            print("!! %s: plain shader instead, missing %s"
                  % (name, [k for k, v in got.items() if v is None]))
            template, slots, two, uv_d = template_mat, (), None, None

        mat = template.copy()
        mat.name = name
        # Blend factor indices follow the engine's own list: the default
        # material reads ONE/ZERO and stores 1/0, so ZERO=0, ONE=1,
        # SRCCOLOR=2, INVSRCCOLOR=3, SRCALPHA=4, INVSRCALPHA=5.
        # AC stores its own lighting coefficients per material. Using them
        # instead of one global value is what keeps flat surfaces from burning
        # out: asphalt asks for a specular of 0.02, not the 0.75 default.
        # One lighting path for every material. AC's coefficients go where rF2
        # applies them -- in the light -- rather than being folded into the
        # texture for composed materials and into the material for the rest,
        # which lit the two by different formulas and darkened every composed
        # surface's ambient by its own diffuse coefficient.
        d = float(pr.get("ksDiffuse", 0.0)) or diffuse_level
        a = float(pr.get("ksAmbient", 0.0)) or ambient_level
        # The racing line rubber keeps AC's own figure. It is an overlay whose
        # job is to darken the road beneath it, and it is the one thing in the
        # scene that reads by being much darker than its surroundings, so
        # lifting it toward rF2's convention is the one change that would
        # undo it. Everything else moves.
        native = "groove" not in row["ac_material"].lower()
        gain = diffuse_scale
        # AC hands the road a separate multiplier for how wet-looking it is.
        spec = (float(pr.get("ksSpecular", 0.0))
                * (float(pr.get("tarmacSpecularMultiplier", 0.0)) or 1.0))
        # Without a txMaps AC masks specular by the diffuse alpha, which rF2
        # never sees: it gets a flat specular map. Silverstone's grass asked
        # for 1.0 against an alpha averaging 0.23, and shone four times as
        # hard as in AC. Not for cut-outs and blends, whose alpha is coverage.
        # The road carries that mask as its own specular map (bake_layers).
        if (not maps and not row.get("alpha_test") and not row.get("needs_blend")
                and name in spec_mask and not (two and two.get("spec"))):
            spec *= spec_mask[name]
        exp = float(pr.get("ksSpecularEXP", 0.0)) or 10.0
        # rF2's own content leaves these at 1.0 and lets the scene light do
        # the work: the texture is the albedo. AC instead states a reflectance
        # per material, 0.13 for rubber against 0.38 for asphalt. Writing 1.0
        # everywhere would put rubber and concrete on the same footing and the
        # racing line would vanish; writing AC's figure straight in darkens
        # every surface to a fraction of what rF2 content does, which is what
        # we have been fighting with gains and clamps for two days. Normalising
        # against the track's own median does both: the middle of the range
        # lands on rF2's convention and AC's relative intent survives.
        # rF2's own convention, measured: all 75 materials in Joesville and
        # Loch Drummond carry ambient and diffuse 1.0, with no variation at
        # all. The texture is the albedo and the scene light does the rest.
        #
        # We were writing AC's coefficients normalised against this track's
        # upper quartile, which lifts the whole track 2.38x and clamps the top
        # 6 materials at 1.0. Measured against the albedo of 342 official
        # textures, that puts 71.3% of our drawn triangles inside their
        # p25-p75 band; 1.0/1.0 puts 85.2% there. The materials it rescues are
        # the ones that were reported wrong: the grass goes from an effective
        # albedo of 42.6 to 71.5 against their grass at 64.8, and the tree
        # canopies from 20.6 to 57.8 against their foliage at 55.
        #
        # It costs the two materials whose AC texture is already brighter than
        # rF2 ever authors: bark and the concrete barriers go from 93 to 130.
        # AC hides that behind a low ksDiffuse; rF2 has nowhere to put it.
        #
        # A flat 1.0 is right where the texture is already an rF2-ish albedo,
        # and wrong where AC's is much brighter and AC hides it behind a low
        # ksDiffuse: bark, the concrete barriers and the chrome on the
        # corkscrew buildings came out at 130, 130 and 255 against an official
        # median of 78. So aim the reflectance at rF2's band using the albedo
        # the texture actually has, and never above 1.0.
        #
        # Left alone: two stage materials, whose first stage is a base the
        # detail modulates rather than the final albedo, and anything on the
        # track surface or flagged as a decal -- every road marking is one or
        # the other, and they are meant to be bright.
        # Flat or upright, measured from face normals rather than guessed at
        # from the name. Flat ground -- the road, the curbs, the markings, the
        # sand and the terrain -- keeps rF2's plain 1.0: those are the
        # surfaces already signed off, and a two stage base is modulated by
        # its detail rather than being the final albedo. Upright structures
        # are what came out too bright, and every one the user named is
        # upright: the corkscrew buildings, the grandstand seating, the
        # concrete barriers and the tree trunks.
        # Flat is not enough on its own: a grandstand's seating tiers face up
        # as squarely as the road does, and gstand_main came out at an albedo
        # of 160 that way. Ground is flat AND either a two stage base, which
        # its detail modulates rather than being the final albedo, or on the
        # racing line. A decal is always left alone.
        flat = bool(row.get("decal")) or (
            float(row.get("flatness", 0.0)) > 0.5
            and (bool(two) or bool(row.get("track_surface"))))
        lum = albedo.get(name)
        # AC's own lighting coefficients, CSP's where it overrides them -- the
        # AC the user compares against -- scaled into rF2 by the one anchor
        # every track shares: Laguna's road, signed off at 1.0 on a texture
        # lifted by ROAD_LIFT. Ambient and diffuse separately, as AC keeps
        # them. Before this the reflectance ignored AC's coefficients for
        # everything upright and aimed the texture at a fixed albedo instead,
        # so glass AC lights at 0.05 came out as bright as a white wall --
        # the "bright blue glass with no transparency".
        kd = float(pr.get("csp_ksDiffuse", d))
        ka = float(pr.get("csp_ksAmbient", a))
        fd = kd / ANCHOR_KD * ROAD_LIFT
        fa = ka / ANCHOR_KA * ROAD_LIFT
        # Brightness the bake already put into the texture: ROAD_LIFT on the
        # road under the rubber, the terrain's lift where AC lights it above
        # what 1.0 can reach. Laguna's terrain keeps its signed-off 1.0.
        lifted = float((two or {}).get("lift") or 0.0)
        if lifted:
            fd, fa = ((fd / lifted, fa / lifted) if row.get("under_rubber")
                      else (1.0, 1.0))
        # Vegetation takes the frame as it stands, with no ceiling: AC's tree
        # and grass shaders, or a cut-out CSP configures. It is what CSP's
        # summer figures darken, and the ceiling was holding it bright.
        vegetation = (row.get("shader") in ("ksTree", "ksGrass")
                      or (name in cutout and not row.get("backdrop")
                          and "csp_ksDiffuse" in pr))
        if native and lum and not flat and not vegetation and name not in dim_scale:
            # Never brighter than the albedo ceiling held these to: AC's tone
            # mapping rolls a white wall off where rF2 clips it. Only darker,
            # where AC's own coefficients say so.
            if name in cutout and not row.get("backdrop"):
                cap = max(0.2, min(1.0, CUTOUT_ALBEDO / lum))
            else:
                # Upright and opaque: aim the brightest tenth of what is drawn,
                # not the average. Bringing the average to 70 left the pit
                # garages' white walls at 104 and the white trucks at 163.
                bright = albedo_p90.get(name) or lum
                cap = max(0.15, min(1.0, SURFACE_ALBEDO / max(bright, lum)))
            if row.get("backdrop"):
                # The painted horizon has no lighting of its own to match:
                # left where it was signed off.
                fd = fa = cap
            else:
                fd, fa = min(fd, cap), min(fa, cap)
        # Grass, darker than AC's coefficients alone put it: the user's
        # standing complaint on every track, with the frame above matching
        # AC's numbers. What the frame cannot see is GrassFX. With CSP the
        # ground under it is covered in shaded, self-shadowed blades and the
        # cards among them read darker with it; rF2 draws the bare texture.
        # An approximation by intent, not a measurement: grass cards (cut-out
        # vegetation AC authors with straight-up normals, which trees are
        # not) by GRASS_CARD_DIM, and the ground CSP grows GrassFX on by
        # GRASS_GROUND_DIM where it is green -- sand and gravel are on those
        # lists too, and grass only grows on their green parts.
        rgb = albedo_rgb.get(name)
        # Laguna's BBgrass has real face normals on ksPerPixelAT, so a card
        # under a metre tall on a cut-out or grass shader counts too: grass
        # measures 0.1-0.8 m, bushes 0.9-1.7, trees 2.7 and up. Laguna's
        # canopies have 0.3 m leaf triangles but no alpha test shader.
        grass_card = vegetation and float(row.get("card_height", 99.0)) < 1.0 and (
            float(row.get("ac_normals_up", 0.0)) > 0.5
            or "AT" in (row.get("shader") or "") or row.get("shader") == "ksGrass")
        if native and grass_card:
            fd, fa = fd * GRASS_CARD_DIM, fa * GRASS_CARD_DIM
        elif native and row.get("grass_fx") and rgb and rgb[1] > max(rgb[0], rgb[2]):
            fd, fa = min(1.0, fd) * GRASS_GROUND_DIM, min(1.0, fa) * GRASS_GROUND_DIM
        fd, fa = min(1.0, fd), min(1.0, fa)
        # The racing line rubber takes AC's own coefficient, untouched: 0.130
        # for Groove, as signed off. It is the non-native branch below.
        mat["Diffuse_Color"] = ([fd] * 3 + [1.0]) if native else             [min(d * gain, 1.0)] * 3 + [1.0]
        mat["Ambient_Color"] = ([fa] * 3 + [1.0]) if native else             [min(a, 1.0)] * 3 + [1.0]
        mat["Specular_Color"] = [min(spec, 1.0)] * 3 + [1.0]
        mat["Specular_Power"] = max(1, int(round(exp)))
        # Grazing angle sheen. The template leaves a 4x boost on everything;
        # AC asks for it on 43 materials and for none at all on the rest, and
        # the road it asks for most -- which is a surface you almost only ever
        # see at a grazing angle.
        mat["FresSpecMin"] = float(pr.get("fresnelC", 0.0))
        mat["FresSpecExp"] = float(pr.get("fresnelEXP", 0.0)) or 1.0
        mat["FresSpecMax"] = float(pr.get("fresnelMaxLevel", 0.0))
        # All 75 materials in Joesville and Loch Drummond carry this, race
        # surface and opaque barriers included, so whatever the addon's "Car
        # Body" label means it is simply part of a normal track export. It is
        # the only bit that separates their flag word from the addon default.
        try:
            mat.CarBody = True          # an attribute; the key is ignored
        except Exception:
            mat["Flag_0x00002000"] = 1
        if row.get("needs_blend") and not row.get("alpha_test"):
            mat["Blend"] = 1
            mat["Flag_0x00000020"] = 1
        if row.get("alpha_test") and name in cutout:
            # Opaque, with the threshold alone doing the cutting -- which is
            # how rF2 ships every cut-out it has. 73 of the 75 materials in
            # Joesville and Loch Drummond carry One/Zero, trees, crowds,
            # shrubs and chain link fences among them; only the grass verge
            # and the road lines really blend. Asking for blending here is
            # what drew the whole quad, and the threshold we asked for never
            # arrived anyway: every gmt we have shipped so far came out
            # One/Zero because Blend_Source_List set as a custom key is
            # ignored. So the one thing that was ever wrong was the number.
            alpha_ref[name] = cutout[name]
        elif row.get("needs_blend") and not row.get("alpha_test"):
            mat["Blend_Source_List"] = 4
            mat["Blend_Destination_List"] = 5

        if slots:
            for slot in slots:
                mat.node_tree.nodes[slot].image = got[slot]
            textured += 1
            counts["two stage" if two else "bump"] += 1
            if two:
                two_stage_used.add(name)
            if uv_d:
                uv_d_of[name] = uv_d
        else:
            counts["plain"] += 1
            node = mat.node_tree.nodes.get("sTex0")
            img = image_for(diffuse) if diffuse else None
            if img is None:
                # A material with no texture, or one that could not be found,
                # still needs a file in its slot: left empty, the exporter
                # writes the slot's own name there and rF2 refuses the scene.
                # Plain white leaves the material's colour to do the work, as
                # AC does with an untextured material.
                img = image_for(WHITE_TEX)
            if node is not None and img is not None:
                node.image = img
                textured += 1
        # Linear filtering, which every texture in Joesville and Loch
        # Drummond has and the addon's own template leaves off: their texture
        # flags read 0x00200018 and a fresh node gives 0x00000008. Without it
        # the sampler is not filtering at all.
        for node in mat.node_tree.nodes:
            if node.type != "TEX_IMAGE":
                continue
            # Not on a cut-out. Bilinear filtering interpolates alpha toward
            # the transparent texels around every leaf, so the alpha test
            # erodes each one to its core: the build that turned this on for
            # cut-outs is the one where canopies and bushes went see-through,
            # and every build before it -- point sampled -- drew them solid.
            if name in alpha_ref_names:
                continue
            set_texture_flag(node, 0x10)
        if name in alpha_ref:
            for slot in ("sTex0", "sBase"):
                node = mat.node_tree.nodes.get(slot)
                if node is None:
                    continue
                # As an attribute, not a custom key. Written as a key it never
                # reached the gmt -- Studio 397's trees carry 191 in that slot
                # and ours carried nothing.
                try:
                    node.Alpha_Reference = alpha_ref[name]
                except Exception:
                    node["Alpha_Reference"] = alpha_ref[name]
                # The threshold on its own does nothing. Colour keying is what
                # makes rF2 apply it, and it is set on exactly 18 texture
                # entries across both official tracks -- every one of them a
                # diffuse map, 17 of the 18 at a threshold of 191. That is
                # their whole cut-out set. We had been writing the right
                # number into a test nothing had switched on, which is why
                # trees, grass and fences drew as solid panels however the
                # threshold moved.
                set_texture_flag(node, 0x80)
                break
        materials[name] = mat
    print("materials prepared: %d (%d textured) -- %d two stage, %d bump, "
          "%d plain, %d alpha tested" % (len(materials), textured,
                                         counts["two stage"], counts["bump"],
                                         counts["plain"], len(alpha_ref)))

    total_meshes = sum(1 for m in data["meshes"] if m["mesh"] in by_mesh)
    progress_path = os.path.join(out_dir, "progress.txt")
    before = set(os.listdir(out_dir))
    exported = []
    used_names = set()
    count = 0
    # Meshes too big for a 16 bit index are split, so the number of files
    # written runs ahead of the number of source meshes. Progress counts the
    # source meshes, which is what the total is.
    done = 0

    for path in data["kn5"]:
        k = KN5(path, geometry=True, placement=data.get("placements", {}).get(path))
        for node in k.walk():
            if node.type not in (2, 3) or not node.index_count:
                continue
            row = by_mesh.get(node.name)
            if row is None:
                continue
            # Neither drawn nor collided with: an AC logic marker such as an
            # audio emitter. Silverstone's AC_AUDIO_* carry a bare 3ds Max
            # material with no texture at all, and exporting them wrote the
            # slot name "sTex0" where a filename belongs -- the fault that
            # stops rF2 loading a scene. They have no job in rF2, so they are
            # not exported. Timing strips are generated separately.
            if not row.get("render", True) and not row.get("physical"):
                continue
            name = sanitize(node.name)
            n, i = name, 1
            while n in used_names:
                i += 1
                n = "%s_%d" % (name, i)
            used_names.add(n)

            matrix = AC_TO_RF2 @ world_matrix(node)
            parts = list(split_node(node))
            # Cut the clear sky out of the mesh so what remains can be drawn
            # opaque: it then writes depth and sorts behind the trees. Only
            # where the alpha test is not doing that already. A cut-out
            # backdrop is trimmed per pixel by its colour key, and cutting it
            # geometrically as well multiplied Spa's forest walls 50 to 250
            # fold -- Loft286 went from 4012 triangles to 234747 -- past the
            # 65536 vertices a gmt can hold, so five of them were not
            # exported at all.
            if row.get("backdrop") and row["rf2_material"] not in alpha_ref:
                tex = row["textures"].get("txDiffuse")
                if tex:
                    base, ext = os.path.splitext(tex)
                    path_tex = os.path.join(tex_dir, sanitize(base) + ext)
                    if os.path.exists(path_tex):
                        cut = []
                        gone = 0
                        for v, u, f, *_n in parts:
                            v2, u2, f2, d = backdrop.clip(v, u, f, path_tex)
                            gone += d
                            if f2:
                                cut.append((v2, u2, f2))
                        if cut:
                            before_t = sum(len(g[2]) for g in parts)
                            after_t = sum(len(f) for _v, _u, f in cut)
                            print("backdrop %s: %d tris -> %d, %d clear dropped"
                                  % (n, before_t, after_t, gone))
                            parts = cut
            for pi, geom in enumerate(parts):
                pname = n if len(parts) == 1 else "%s_p%d" % (n, pi + 1)
                count += 1
                empty, ob = build_mesh(geom, matrix, pname,
                                       materials[row["rf2_material"]],
                                       template_empty, template_ob, coll,
                                       float(row.get("uv_repeat", 1.0)),
                                       ((row.get("two_stage") or {}).get("uv_b")
                                        if row["rf2_material"] in two_stage_used
                                        else None),
                                       uv_d_of.get(row["rf2_material"]),
                                       DECAL_LIFT if row.get("decal") else 0.0)
                for o in bpy.data.objects:
                    o.select_set(False)
                empty.select_set(True)
                ob.select_set(True)
                bpy.context.view_layer.objects.active = empty
                try:
                    bpy.ops.export_scene.export_gmt_override(
                        directory=out_dir + os.sep,
                        filename=empty.name + ".gmt",
                        check_existing=False)
                    now = set(os.listdir(out_dir))
                    # Look for the file we asked for. Diffing the directory is
                    # not enough: the addon renames on collisions, so the new
                    # entry can carry a name we never chose and be missed.
                    want = (pname + ".gmt").lower()
                    match = next((f for f in now if f.lower() == want), None)
                    new = sorted(now - before)
                    before = now
                    if match or new:
                        exported.append({
                            "mesh": row["mesh"],
                            "gmt": match or new[0],
                            "material": row["rf2_material"],
                            "physical": row["physical"],
                            "render": row.get("render", True),
                            "lod_in": row.get("lod_in", 0.0),
                            "lod_out": row.get("lod_out", 0.0),
                            "blend": row.get("needs_blend", False),
                            "radius": row.get("radius", 0.0),
                            "tris": row["tris"],
                        })
                    else:
                        print("!! no file produced for %s" % pname)
                except Exception as exc:
                    print("!! export %s: %s" % (pname, str(exc)[:120]))

                # Free immediately. Holding 300 meshes of this size at once
                # crashes Blender.
                mesh_data = ob.data
                bpy.data.objects.remove(ob, do_unlink=True)
                bpy.data.objects.remove(empty, do_unlink=True)
                bpy.data.meshes.remove(mesh_data, do_unlink=True)
            done += 1
            if done % 25 == 0:
                pct = 100.0 * done / max(1, total_meshes)
                line = "progress %d/%d meshes (%.0f%%)" % (done, total_meshes, pct)
                print(line, flush=True)
                try:
                    with open(progress_path, "w") as pf:
                        pf.write(line + chr(10))
                except Exception:
                    pass
            if limit and count >= limit:
                break
        if limit and count >= limit:
            break

    # Timing trigger strips, if make_aiw.py produced them.
    extras_path = os.path.join(os.path.dirname(map_path), "sectors.json")
    if os.path.exists(extras_path) and not limit:
        # Timing strips must be crossed but never seen, so they get a fully
        # transparent texture rather than borrowing a visible one.
        clear_img = image_for("_invisible.dds")
        trig_mat = template_mat.copy()
        trig_mat.name = "road_timing"
        trig_mat["Blend"] = 1
        trig_mat["Flag_0x00000020"] = 1
        trig_mat["Blend_Source_List"] = 4
        trig_mat["Blend_Destination_List"] = 5
        node = trig_mat.node_tree.nodes.get("sTex0")
        if node is not None and clear_img is not None:
            node.image = clear_img
        for spec in json.load(open(extras_path)):
            verts = [tuple(v) for v in spec["verts"]]
            uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
            geom = (verts, uvs, [tuple(f) for f in spec["faces"]])
            empty, ob = build_mesh(geom, AC_TO_RF2, spec["name"],
                                   trig_mat, template_empty, template_ob, coll)
            for o in bpy.data.objects:
                o.select_set(False)
            empty.select_set(True)
            ob.select_set(True)
            bpy.context.view_layer.objects.active = empty
            try:
                bpy.ops.export_scene.export_gmt_override(
                    directory=out_dir + os.sep,
                    filename=spec["name"] + ".gmt",
                    check_existing=False)
                now = set(os.listdir(out_dir))
                want = (spec["name"] + ".gmt").lower()
                match = next((f for f in now if f.lower() == want), None)
                new = sorted(now - before)
                before = now
                if match or new:
                    exported.append({"mesh": spec["name"], "gmt": match or new[0],
                                     "material": "road_timing",
                                     "physical": True, "timing": True,
                                     "tris": 2})
                    print("timing poly %s -> %s" % (spec["name"], match or new[0]))
                else:
                    print("!! no file produced for %s" % spec["name"])
            except Exception as exc:
                print("!! export %s: %s" % (spec["name"], str(exc)[:120]))
            mesh_data = ob.data
            bpy.data.objects.remove(ob, do_unlink=True)
            bpy.data.objects.remove(empty, do_unlink=True)
            bpy.data.meshes.remove(mesh_data, do_unlink=True)

    with open(os.path.join(out_dir, "exported.json"), "w") as f:
        json.dump(exported, f, indent=1)
    print("EXPORTED %d of %d" % (len(exported), count))


if __name__ == "__main__":
    main()
