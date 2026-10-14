"""Find the texture property that sets Color_Key (texture flag 0x80).

Official cut-outs carry 0x80 in the texture flags dword and ours do not,
which is why our alpha reference is stored and never applied. The addon
exposes texture flags under several plausible names; try each on its own
mesh and report which one actually moves the bit in the exported gmt.

blender --background --factory-startup --python probe_colorkey.py -- <tex_dir> <out_dir>
"""
import os
import sys
import struct
import importlib

import bpy

args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
TEX_DIR = args[0]
OUT_DIR = args[1]
SHADER = "L2BUMPSPECMAPT0"
SLOTS = ("sBase", "sSpec", "sBump")
CANDIDATES = ["Flag_0x00000010", "Flag_0x00000010_Linear_Filter",
              "Linear_Filter", "LinearFilter", "Filter", "Flag_0x00200000"]


def line(*a):
    print(*a)
    sys.stdout.flush()


def main():
    bpy.ops.preferences.addon_enable(module="io_rf2_gmt_release")
    nodes = importlib.import_module("io_rf2_gmt_release.rF2_Nodes_Stuff")
    pick = sorted(f for f in os.listdir(TEX_DIR)
                  if f.lower().endswith(".dds"))[:1]
    if not pick:
        line("!! no textures in %s" % TEX_DIR)
        return
    img = bpy.data.images.load(os.path.join(TEX_DIR, pick[0]),
                              check_existing=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    for idx, prop in enumerate(["NONE"] + CANDIDATES):
        bpy.ops.mesh.primitive_cube_add()
        bpy.ops.mesh.add_rf2_object()
        obs = [o for o in bpy.data.objects
               if o.type == "MESH" and o.data.materials
               and o.data.materials[0] and o.parent]
        ob = obs[-1]
        empty = ob.parent
        mat = ob.data.materials[0]
        mat, ob = nodes.CreateNodeTree(mat, ob, "BUMP", SHADER)
        node = mat.node_tree.nodes.get("sBase")
        for s in SLOTS:
            n = mat.node_tree.nodes.get(s)
            if n is not None:
                n.image = img
        # the threshold, which we already know lands
        try:
            node.Alpha_Reference = 191
        except Exception as exc:
            line("   Alpha_Reference failed: %s" % exc)
        how = "none"
        if prop != "NONE":
            ok_attr = ok_key = False
            try:
                setattr(node, prop, True)
                ok_attr = True
            except Exception:
                pass
            try:
                node[prop] = 1
                ok_key = True
            except Exception:
                pass
            how = "attr=%s key=%s" % (ok_attr, ok_key)
        name = "ck%d_%s" % (idx, prop)
        # The exporter names the file from the empty's name, via its own
        # File_Name_Update hook, so set the name as well as the property.
        empty.name = name
        empty["File_Name"] = name
        # The exporter only writes objects it recognises as rF2 ones.
        empty.rFactor = "2B"
        ob.rFactor = "2B"
        for o in bpy.data.objects:
            o.select_set(False)
        empty.select_set(True)
        ob.select_set(True)
        bpy.context.view_layer.objects.active = empty
        try:
            bpy.ops.export_scene.export_gmt_override(directory=OUT_DIR + os.sep)
        except Exception as exc:
            line("!! export %s: %s" % (name, exc))
            continue
        p = os.path.join(OUT_DIR, name + ".gmt")
        if not os.path.exists(p):
            made = [f for f in os.listdir(OUT_DIR) if f.lower().endswith(".gmt")]
            if not made:
                line("%-32s no gmt written" % prop)
                continue
            p = os.path.join(OUT_DIR, sorted(
                made, key=lambda f: os.path.getmtime(os.path.join(OUT_DIR, f)))[-1])
        b = open(p, "rb").read()
        i = b.find(pick[0].encode())
        if i < 0:
            line("%-32s texture name not found" % prop)
            continue
        flags = struct.unpack_from("<I", b, i + 64)[0]
        ref = b[i + 100]
        line("%-32s flags=0x%08x  0x10=%-5s ref=%-4d  (%s)"
             % (prop, flags, bool(flags & 0x10), ref, how))


if __name__ == "__main__":
    main()
