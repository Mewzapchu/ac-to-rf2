"""Check an installed track against what the pipeline meant to produce.

Written after two rounds of reporting a fix as verified when it was not. The
checks here read the files rF2 will actually load and compare them against the
material map, rather than searching for a value somewhere in the neighbourhood
and calling a coincidence a confirmation.

    python verify_install.py "<rF2>\\ModDev\\Locations\\<track>" out/<track>
"""
import os
import re
import sys
import json
import struct

# Measured by exporting one mesh twice with different thresholds and diffing:
# exactly one byte moved. From the START of the texture name, not the end --
# the name sits in a fixed size field, so measuring from the end shifts with
# every filename length and reads the wrong byte. Checked against two names of
# different lengths, 13 and 21 characters, both at 100.
ALPHA_REF_OFFSET = 100

# From the same fixed-size name field: the texture flags dword. Colour keying
# (0x80) is what makes rF2 apply the alpha reference at all. Across both
# official tracks it is set on 18 texture entries, every one a diffuse map and
# 17 of them at a threshold of 191 -- their entire cut-out set. We shipped the
# right threshold with this bit clear and every cut-out drew as a solid panel.
TEX_FLAGS_OFFSET = 64
COLOR_KEY = 0x80

SLOT_NAMES = {"sBase", "sBase1", "sSpec", "sBump", "sTex0", "sCube", "sTex1"}

# L2DIFFUSET0 carries no alpha test, so a cut-out written with it draws as the
# whole quad however correct its threshold is. Studio 397 put every tree,
# shrub and treeline on a bump shader and none on the plain one.
NO_ALPHA_TEST = {"L2DIFFUSET0"}
KNOWN_SHADERS = ("L2BUMPSPECMAPT0XT1", "L2BUMPSPECMAPFACESTAMPT0",
                 "L2BUMPSPECMAPT0", "L2SPECULARMAPT0XT1", "L2SPECULARMAPT0",
                 "L2BUMPT0", "L2DIFFUSET0")


def shader_of(data):
    """The shader a gmt was written with, matched whole.

    Vertex data throws up runs like L2M2N2O2P2 that start with L2 and pass a
    loose pattern, which is how a block of coordinates once got read as a
    material.
    """
    for name in KNOWN_SHADERS:
        raw = name.encode()
        i = data.find(raw)
        while i >= 0:
            end = i + len(raw)
            if end >= len(data) or not (65 <= data[end] <= 90
                                        or 48 <= data[end] <= 57):
                return name
            i = data.find(raw, i + 1)
    return None


def material_colours(data, shader_at):
    """Ambient and diffuse sit at fixed offsets before the shader name."""
    f = lambda o: struct.unpack_from("<f", data, shader_at + o)[0]
    return f(-80), f(-64)


def read_gmt(path):
    b = open(path, "rb").read()
    out = {"shader": shader_of(b),
           "textures": set(re.findall(rb"[A-Za-z0-9_\-]+\.dds", b)),
           "strings": set(x.group().decode()
                          for x in re.finditer(rb"[ -~]{3,}", b)),
           "materials": [], "alpha_refs": {}}
    for m in re.finditer(rb"L2[A-Z0-9]{6,40}", b):
        s = m.start()
        if s < 96:
            continue
        amb, dif = material_colours(b, s)
        if 0.0 <= amb <= 1.0 and 0.0 <= dif <= 1.0:
            out["materials"].append((m.group().decode(), amb, dif))
    out["tex_flags"] = {}
    for m in re.finditer(rb"[A-Za-z0-9_\-]+\.dds", b):
        at = m.start() + ALPHA_REF_OFFSET
        if at < len(b):
            out["alpha_refs"][m.group().decode()] = b[at]
        fl = m.start() + TEX_FLAGS_OFFSET
        if fl + 4 <= len(b):
            out["tex_flags"][m.group().decode()] =                 struct.unpack_from("<I", b, fl)[0]
    return out


def vertical_normal_share(path, limit=4000):
    """Share of vertex normals in a gmt that point straight up.

    Vertex records hold a position and then its normal, at a fixed stride (96
    bytes on the bump shaders). A position sits metres from the origin and a
    normal has length one, so a float triple of length well over one followed
    by one of length one is a candidate; other fields inside a record pass
    that test too, so only candidates spaced at the dominant stride count.
    None when too few are found to say anything.
    """
    b = open(path, "rb").read()
    hits = []
    for off in range(0, len(b) - 24, 4):
        px, py, pz = struct.unpack_from("<3f", b, off)
        if not (4.0 < abs(px) + abs(py) + abs(pz) < 1e5):
            continue
        nx, ny, nz = struct.unpack_from("<3f", b, off + 12)
        # Written so a NaN fails: every comparison with one is false.
        if not abs(nx * nx + ny * ny + nz * nz - 1.0) <= 1e-3:
            continue
        hits.append((off, ny))
        if len(hits) >= limit:
            break
    if len(hits) < 8:
        return None
    gaps = {}
    for (a, _), (c, _) in zip(hits, hits[1:]):
        gaps[c - a] = gaps.get(c - a, 0) + 1
    stride = max(gaps, key=gaps.get)
    offs = {o for o, _ in hits}
    rec = [ny for o, ny in hits if o + stride in offs or o - stride in offs]
    if len(rec) < 8:
        return None
    return sum(ny > 0.99 for ny in rec) / float(len(rec))


def main():
    installed, out_dir = sys.argv[1], sys.argv[2]
    gmt_dir = os.path.join(installed, "Assets", "GMTs")
    map_dir = os.path.join(installed, "Assets", "MAPs")
    venue = os.path.basename(installed.rstrip("\\/"))
    scn = os.path.join(installed, venue, venue + ".scn")
    data = json.load(open(os.path.join(out_dir, "material_map.json")))

    rows = {m["mesh"]: m for m in data["meshes"]}
    on_disk = set(os.listdir(map_dir))
    scene = open(scn, encoding="utf-8", errors="replace").read()

    problems = []

    # 1. every texture a mesh asks for is actually installed, and no slot name
    #    ended up where a filename belongs
    missing, leaked = set(), []
    refs = {}
    for f in os.listdir(gmt_dir):
        if not f.lower().endswith(".gmt"):
            continue
        g = read_gmt(os.path.join(gmt_dir, f))
        for t in g["textures"]:
            if t.decode() not in on_disk:
                missing.add(t.decode())
        leaked += [(f, s) for s in SLOT_NAMES & g["strings"]]
        refs[f] = g
    if missing:
        problems.append("textures referenced but not installed: %s" % sorted(missing)[:5])
    if leaked:
        problems.append("slot names written where filenames belong: %s" % leaked[:5])

    # 2. the detail levels we decided not to draw are really not drawn
    hidden = set(re.findall(r"Instance=(\S+)\s*\{[^}]*LODOut=\(0\.01\)", scene))
    want_hidden = {m["mesh"] for m in data["meshes"] if m.get("lod_of")}
    want_hidden |= {m["mesh"] for m in data["meshes"] if not m.get("render", True)}
    # Only meshes that were exported at all can be drawn; one that is neither
    # drawn nor collidable is now left out of the export entirely.
    rec_path = os.path.join(out_dir, "gmt", "exported.json")
    shipped = ({r.get("mesh") for r in json.load(open(rec_path))}
               if os.path.exists(rec_path) else set(want_hidden))
    still_drawn = [n for n in want_hidden if n in shipped
                   and re.sub(r"[^A-Za-z0-9_]", "_", n) not in hidden]
    if still_drawn:
        problems.append("meant to be hidden but still drawn: %d, e.g. %s"
                        % (len(still_drawn), still_drawn[:4]))

    # 3. alpha thresholds reached the file, at the offset they are read from
    # The thresholds the bake decided from the shipping textures. AC's
    # ksAlphaRef is not in this any more, so checking against it would fail
    # every cut-out we just fixed.
    cutout = data.get("alpha_ref", {})
    want_ref = {}
    for m in data["meshes"]:
        name = m["rf2_material"]
        if name not in cutout:
            continue
        t = m.get("baked") or m["textures"].get("txDiffuse")
        if not t:
            continue
        disk = data.get("cutout_tex", {}).get(name) or (
            t if m.get("baked") else data.get("texture_files", {}).get(t, t))
        want_ref[disk] = cutout[name]
    # Per gmt, not per texture name. A texture shared with a material that is
    # not alpha tested would otherwise be checked against a threshold that was
    # never meant to apply to it, and reported as wrong.
    exported = {}
    rec = os.path.join(out_dir, "gmt", "exported.json")
    if os.path.exists(rec):
        exported = {r["gmt"]: r.get("mesh") for r in json.load(open(rec))}
    else:
        # Say so rather than quietly checking nothing and reporting no faults,
        # which is the failure this whole script exists to stop.
        problems.append("no export record at %s, so nothing could be checked "
                        "against the meshes -- run this after a build finishes"
                        % rec)
    checked = wrong = 0
    examples = []
    for fname, g in refs.items():
        mesh = exported.get(fname)
        row = rows.get(mesh) if mesh else None
        if row is None or row["rf2_material"] not in cutout:
            continue
        t = row.get("baked") or row["textures"].get("txDiffuse")
        disk = data.get("cutout_tex", {}).get(row["rf2_material"]) or (
            t if row.get("baked") else data.get("texture_files", {}).get(t, t))
        if disk not in g["alpha_refs"]:
            continue
        # From this mesh's own material. One texture can serve two materials
        # with different thresholds -- BBGRASS_D is the grass at 26 and the
        # bushes at 178 -- so keying the expectation by texture name judges one
        # of them against the other's figure.
        want = cutout[row["rf2_material"]]
        checked += 1
        got = g["alpha_refs"][disk]
        if got != want:
            wrong += 1
            if len(examples) < 4:
                examples.append("%s %s got %d want %d" % (fname, disk, got, want))
    # 4. the threshold is switched on. Without colour keying it is inert and
    #    the cut-out draws whole, which is the bug this check exists for.
    unkeyed = []
    for fname, g in refs.items():
        mesh = exported.get(fname)
        row = rows.get(mesh) if mesh else None
        if row is None or row["rf2_material"] not in cutout:
            continue
        t = row.get("baked") or row["textures"].get("txDiffuse")
        disk = data.get("cutout_tex", {}).get(row["rf2_material"]) or (
            t if row.get("baked") else data.get("texture_files", {}).get(t, t))
        fl = g["tex_flags"].get(disk)
        if fl is not None and not fl & COLOR_KEY:
            unkeyed.append("%s %s 0x%08x" % (fname, disk, fl))
    if unkeyed:
        problems.append("cut-outs whose alpha test is not switched on "
                        "(no colour key, so the threshold is ignored and the "
                        "whole quad draws): %d, e.g. %s"
                        % (len(unkeyed), unkeyed[:3]))

    # 5. a cut-out on a shader that cannot test alpha draws as a solid quad
    blind = []
    for fname, g in refs.items():
        mesh = exported.get(fname)
        row = rows.get(mesh) if mesh else None
        if (row is not None and row["rf2_material"] in cutout
                and g["shader"] in NO_ALPHA_TEST):
            blind.append("%s on %s" % (fname, g["shader"]))
    if blind:
        problems.append("alpha tested meshes on a shader with no alpha test: "
                        "%d, e.g. %s" % (len(blind), blind[:3]))

    # 6. upright surfaces carry their own normals, not "straight up". The
    #    exporter's Normals property once sat on the branch that writes the
    #    constant (0, 0, 1) for every vertex, which lit every wall, ceiling and
    #    post on the track as if it faced the sky. A vertex record is a
    #    position followed by its normal; walls whose normals all point up are
    #    that fault coming back.
    sky_lit, walls = [], 0
    for fname, g in refs.items():
        mesh = exported.get(fname)
        row = rows.get(mesh) if mesh else None
        if (row is None or float(row.get("upward", 1.0)) > 0.1
                or float(row.get("ac_normals_up", 0.0)) > 0.5
                or row.get("shader") in ("ksTree", "ksGrass")
                or row["rf2_material"] in cutout):
            continue
        ups = vertical_normal_share(os.path.join(gmt_dir, fname))
        if ups is None:
            continue
        walls += 1
        if ups > 0.9:
            sky_lit.append("%s %.0f%% up" % (fname, 100 * ups))
    if sky_lit:
        problems.append("upright meshes whose normals all point up, so they are "
                        "lit as if they faced the sky: %d of %d, e.g. %s"
                        % (len(sky_lit), walls, sky_lit[:3]))

    # 7. the visible range in each gmt (LOD_Out, a float at offset 260 after
    #    LOD_In at 256). The exporter's default is 1000 m, which culled Spa's
    #    far trees and its 1.35 km rubber meshes in and out as you drove.
    short_range = []
    for fname in refs:
        try:
            with open(os.path.join(gmt_dir, fname), "rb") as fh:
                head = fh.read(264)
            if len(head) == 264 and struct.unpack_from("<f", head, 260)[0] <= 1000.0:
                short_range.append(fname)
        except OSError:
            pass
    if short_range:
        problems.append("%d gmts keep the exporter's 1000 m visible range, e.g. %s"
                        % (len(short_range), short_range[:3]))

    # 8. something casts a shadow. The scene once named no caster at all.
    casters = scene.count("ShadowCaster=")
    if not casters:
        problems.append("no instance casts a shadow")

    if checked and wrong:
        problems.append("alpha thresholds wrong in %d of %d gmts: %s"
                        % (wrong, checked, examples))
    if want_ref and not checked:
        problems.append("no alpha threshold found in any gmt, expected %d" % len(want_ref))

    print("installed:   %s" % installed)
    print("gmts:        %d" % len(refs))
    print("textures:    %d installed, %d missing" % (len(on_disk), len(missing)))
    print("hidden:      %d instances, %d wanted" % (len(hidden), len(want_hidden)))
    print("alpha tests: %d checked, %d wrong, %d not colour keyed, %d on a "
          "shader that cannot test" % (checked, wrong, len(unkeyed), len(blind)))
    print("normals:     %d upright meshes checked, %d lit as if facing up"
          % (walls, len(sky_lit)))
    print("ranges:      %d gmts at the 1000 m default" % len(short_range))
    print("shadows:     %d casters" % casters)
    for line in scene.splitlines():
        if line.startswith(("AmbientColor", "FogMode")) or "Type=Directional" in line:
            print("scene:       %s" % line.strip())
    print()
    if problems:
        print("PROBLEMS")
        for p in problems:
            print("  - %s" % p)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
