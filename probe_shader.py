"""Build a two-diffuse rF2 material and check it survives the exporter.

rF2's L2BUMPSPECMAPT0XT1 shader multiplies two diffuse textures, each sampled
through its own UV layer. That is what an AC road material actually is: a base
plus a detail tiled far finer, and it is the one thing a single baked texture
can never reproduce, however large we make it.

Run:  blender --background --factory-startup --python probe_shader.py -- <tex_dir> <out_dir>

Exports one cube with the shader and reports what the gmt came out holding.
"""
import os
import sys
import importlib

import bpy

SHADER = "L2BUMPSPECMAPT0XT1"

args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
TEX_DIR = args[0] if args else ""
OUT_DIR = args[1] if len(args) > 1 else os.path.abspath("probe_out")


def line(*a):
    print(*a)
    sys.stdout.flush()


def main():
    bpy.ops.preferences.addon_enable(module="io_rf2_gmt_release")
    nodes_mod = importlib.import_module("io_rf2_gmt_release.rF2_Nodes_Stuff")

    bpy.ops.mesh.primitive_cube_add()
    bpy.ops.mesh.add_rf2_object()
    obs = [o for o in bpy.data.objects
           if o.type == "MESH" and o.data.materials and o.data.materials[0]]
    ob = obs[-1]
    empty = ob.parent
    mat = ob.data.materials[0]
    line("before: Shader_Name=%r nodes=%s" % (mat.Shader_Name,
                                              [n.name for n in mat.node_tree.nodes]))

    # The group is one of the plain strings the function compares against, and
    # the second argument is the object, not its mesh data.
    try:
        mat, ob = nodes_mod.CreateNodeTree(mat, ob, "BUMP", SHADER)
        line("CreateNodeTree ok")
    except Exception as exc:
        import traceback
        line("!! CreateNodeTree: %s" % exc)
        traceback.print_exc()
        return
    line("after:  Shader_Name=%r nodes=%s" % (mat.Shader_Name,
                                              [n.name for n in mat.node_tree.nodes]))

    # Two different textures so the gmt can be checked for both.
    picks = sorted(f for f in os.listdir(TEX_DIR)
                   if f.lower().endswith(".dds"))[:2] if TEX_DIR else []
    line("textures: %s" % picks)
    for slot, fn in zip(("sBase", "sBase1"), picks):
        node = mat.node_tree.nodes.get(slot)
        if node is None:
            line("!! no node %s" % slot)
            continue
        node.image = bpy.data.images.load(os.path.join(TEX_DIR, fn),
                                          check_existing=True)
        line("  %s <- %s" % (slot, fn))

    # Tile the detail four times against the base to prove the UV sets are
    # read separately rather than shared.
    me = ob.data
    scale = float(os.environ.get("UVB_SCALE", "4"))
    if "UV_B" in me.uv_layers:
        for d in me.uv_layers["UV_B"].data:
            d.uv = (d.uv[0] * scale, d.uv[1] * scale)
        line("UV_B scaled x%g" % scale)

    os.makedirs(OUT_DIR, exist_ok=True)
    empty["File_Name"] = "probe_t1t2"
    for o in bpy.data.objects:
        o.select_set(False)
    empty.select_set(True)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = empty
    try:
        bpy.ops.export_scene.export_gmt_override(directory=OUT_DIR + os.sep)
    except Exception as exc:
        line("!! export: %s" % exc)

    for f in sorted(os.listdir(OUT_DIR)):
        p = os.path.join(OUT_DIR, f)
        data = open(p, "rb").read()
        found = [n for n in picks if n.encode() in data
                 or os.path.splitext(n)[0].encode() in data]
        line("gmt %s: %d bytes, textures present: %s" % (f, len(data), found))
    line("probe done")


if __name__ == "__main__":
    main()
