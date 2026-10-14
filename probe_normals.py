"""What normals does the gmt exporter write, in each Normals mode?

Disassembling the addon's process_polygon (rF2_GMT_Export.pyc, Python 3.10)
shows the vertex normal is chosen by the mesh's Normals property:

    '0'   -> loop.normal (Blender's split normals, custom normals included)
    '1'   -> the polygon's face normal
    else  -> the constant (0, 0, 1): straight up, whatever the surface

The geometry stage had been setting 2, which lands in the last branch, so
every wall, ceiling and post was lit as though it faced the sky.

This exports a wall and a floor quad, in AC coordinates, through
blender_stage.build_mesh with each mode, plus mode 0 with AC normals, and
reads the normals back from the vertex block of each gmt. The vertex record
is position (3 floats) followed by the normal (3 floats), written as x, z, y.

blender --background --factory-startup --python probe_normals.py -- <tex_dir> <out_dir>
"""
import os
import sys
import math
import struct

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import blender_stage as bs  # noqa: E402

args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
TEX_DIR = args[0]
OUT_DIR = args[1]


def line(*a):
    print(*a)
    sys.stdout.flush()


def normals_after_positions(b, positions):
    """For each known vertex position, the 3 floats that follow it in the file."""
    out = []
    for p in positions:
        for off in range(0, len(b) - 24, 4):
            x, y, z = struct.unpack_from("<3f", b, off)
            if abs(x - p[0]) < 1e-4 and abs(y - p[1]) < 1e-4 and abs(z - p[2]) < 1e-4:
                n = struct.unpack_from("<3f", b, off + 12)
                if abs(sum(c * c for c in n) - 1.0) < 1e-2:
                    out.append(tuple(round(c, 2) for c in n))
                    break
    return out


def main():
    bpy.ops.preferences.addon_enable(module="io_rf2_gmt_release")
    bs.silence_popups()
    empty_t, ob_t, mat_t, coll = bs.make_template()
    pick = sorted(f for f in os.listdir(TEX_DIR) if f.lower().endswith(".dds"))[:1]
    img = bpy.data.images.load(os.path.join(TEX_DIR, pick[0]), check_existing=True)
    node = mat_t.node_tree.nodes.get("sTex0")
    if node is not None:
        node.image = img
    os.makedirs(OUT_DIR, exist_ok=True)
    # AC coordinates, Y up. A wall in the Y-Z plane whose front faces AC +X
    # (counter-clockwise seen from +X), and a floor facing up. Positions are
    # distinct so each can be found in the file.
    wall = ([(5.0, 0.0, 0.0), (5.0, 0.0, -1.0), (5.0, 1.0, -1.0), (5.0, 1.0, 0.0)],
            [(0, 0), (1, 0), (1, 1), (0, 1)], [(0, 1, 2), (0, 2, 3)])
    floor = ([(7.0, 0.0, 0.0), (8.0, 0.0, 0.0), (8.0, 0.0, -1.0), (7.0, 0.0, -1.0)],
             [(0, 0), (1, 0), (1, 1), (0, 1)], [(0, 1, 2), (0, 2, 3)])
    for label, geom in (("wall facing AC +X", wall), ("floor facing up", floor)):
        # What AC's own winding says the face normal is, in AC coordinates.
        v = geom[0]
        u = [v[1][i] - v[0][i] for i in range(3)]
        w = [v[2][i] - v[0][i] for i in range(3)]
        fn = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
        line("%s: AC winding normal %s" % (label, fn))
        for mode, extra in (("0", None), ("1", None), ("2", None),
                            ("0", "ac"), ("0", "up")):
            g = geom
            if extra == "ac":
                # AC's authored normal for this face, as AC stores it.
                g = geom + ([fn] * 4,)
            elif extra == "up":
                # Straight up whatever the face, the way AC authors a grass card.
                g = geom + ([(0.0, 1.0, 0.0)] * 4,)
            name = "pn_%s_%s%s" % (label.split()[0], mode, "_" + extra if extra else "")
            empty, ob = bs.build_mesh(g, bs.AC_TO_RF2, name, mat_t, empty_t, ob_t, coll)
            ob.data["Normals"] = int(mode)
            line("   Normals=%s%s reads back as %r" % (mode, " +AC normals " + extra if extra else "",
                                                   ob.data.Normals if hasattr(ob.data, "Normals") else None))
            # Where build_mesh put the positions, in the order the exporter
            # writes them (x, z, y of Blender's coordinates).
            pos = []
            for vt in ob.data.vertices:
                c = vt.co
                pos.append((c.x, c.z, c.y))
            for o in bpy.data.objects:
                o.select_set(False)
            empty.select_set(True)
            ob.select_set(True)
            bpy.context.view_layer.objects.active = empty
            bpy.ops.export_scene.export_gmt_override(
                directory=OUT_DIR + os.sep, filename=name + ".gmt", check_existing=False)
            p = os.path.join(OUT_DIR, name + ".gmt")
            if not os.path.exists(p):
                line("   !! no gmt for %s" % name)
                continue
            b = open(p, "rb").read()
            line("      positions in file %s" % [tuple(round(c, 2) for c in q) for q in pos[:2]])
            line("      normals in file   %s" % sorted(set(normals_after_positions(b, pos))))


if __name__ == "__main__":
    main()
