"""AC track -> rFactor 2 converter, stage 1.

Reads an Assetto Corsa track folder and produces the rF2 side of everything
that is not mesh geometry: surface mapping, terrain definition, scene file,
track database, extracted textures, and a mapping file for the Blender stage.

Geometry export itself needs Traveller's io_rf2_gmt addon in Blender.
"""
import os
import re
import sys
import json
import argparse
import configparser
import struct
from collections import OrderedDict, defaultdict

from kn5 import KN5

# AC surface key -> rF2 TDF material prefix. The rF2 terrain definition matches
# the FIRST PART of a material name, so the prefix is what selects the surface.
SURFACE_MAP = {
    "ROAD": "road",
    "KERB": "rmbl",
    "GRASS": "gras",
    "SAND": "grvl",
    "GRAVEL": "grvl",
    "DIRT": "dstg",
    "WALL": "wall",
    "CONCRETE": "cem",
    "TYREWALL": "twal",
    "GUARDRAIL": "grdr",
    "SAFER": "safer",
}
# AC names its functional markers with an AC_ prefix: grid slots, pit stalls,
# timing gates, audio emitters. They carry real geometry, often a small box,
# and are never meant to be drawn.
DEFAULT_PREFIX = "road"
DIGIT_PREFIX = re.compile(r"^(\d+)(.*)$")



# Alpha blending costs correct depth sorting, so it is only worth paying where
# a texture is genuinely see-through. A distant backdrop is a mostly opaque
# panel with a soft edge: blend it and it paints over the trees in front of it.
# A backdrop is mostly opaque but its sky region must still be see-through,
# so the test is whether a real portion of the texture is transparent.
MIN_TRANSPARENT_PCT = 5.0
# A transparent panel this large is a painted horizon backdrop. It cannot be
# depth sorted against the scene, so it always paints over the trees. rF2
# draws its own sky, so the panel is dropped rather than drawn wrongly.
BACKDROP_RADIUS = 1000.0


def texture_alpha_stats(kn5_paths):
    """Percentage of each embedded DXT5 texture that is fully opaque."""
    stats = {}
    for path in kn5_paths:
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            d = f.read()
        p = 6
        ver = struct.unpack_from("<I", d, p)[0]
        p += 4
        if ver > 5:
            p += 4 + struct.unpack_from("<I", d, p)[0]
        count = struct.unpack_from("<I", d, p)[0]
        p += 4
        for _ in range(count):
            p += 4
            ln = struct.unpack_from("<I", d, p)[0]
            p += 4
            name = d[p:p + ln].decode("utf-8", "replace")
            p += ln
            size = struct.unpack_from("<I", d, p)[0]
            p += 4
            blob = d[p:p + size]
            p += size
            stats[name] = opaque_percent(blob)
    return stats


def opaque_percent(blob):
    """100 means no transparency anywhere; None if we cannot tell."""
    if len(blob) < 132 or blob[:4] != b"DDS " or blob[84:88] != b"DXT5":
        return None
    h, w = struct.unpack_from("<II", blob, 12)
    blocks = max(1, (w + 3) // 4) * max(1, (h + 3) // 4)
    seen = opaque = 0
    for b in range(min(blocks, 60000)):
        off = 128 + b * 16
        if off + 2 > len(blob):
            break
        seen += 1
        if blob[off] >= 250 and blob[off + 1] >= 250:
            opaque += 1
    return 100.0 * opaque / seen if seen else None



REACTIONS = [
    "Reaction=tiresmoke Tex=SMOKETire.tga Max=1024 Scale=(0.5,0.5,0.5) "
    "Growth=(2.5,2.0,2.0) ASDEnvelope=(0.1,0.7,3.8) Suspension=0.98 "
    "DestBlend=InvSrcAlpha SrcBlend=SrcAlpha",
    "Reaction=skid Tex=skidhard.tga Max=2500 Pixel=NoReduceDetail "
    "Particle=Plane+Deformable+SingleSided DestBlend=InvSrcAlpha SrcBlend=SrcAlpha",
    "Reaction=wetskid Tex=skidwet.tga Max=1024 Duration=0.40 Pixel=NoReduceDetail "
    "Particle=Plane+Deformable+SingleSided DestBlend=One SrcBlend=SrcAlpha",
]

LOOSE = [
    "Reaction=spark Tex=spark.tga Max=256 Duration=0.6 Scale=(.08,.05,.05) "
    "Particle=Plane+Deformable DestBlend=InvSrcAlpha SrcBlend=SrcAlpha "
    "Shader=AlphaChannel+NoShade",
    "Reaction=whitesmoke Tex=smokewhite.tga Max=256 Scale=(1.0,1.0,1.0) "
    "Growth=(2.5,2.0,2.0) ASDEnvelope=(1.0,1.2,3.3) DriftMult=0.15 "
    "Suspension=0.999 DestBlend=InvSrcAlpha SrcBlend=SrcAlpha",
    "Reaction=blacksmoke Tex=smokeblack.tga Max=1024 Scale=(1.5,1.5,1.5) "
    "Growth=(2.0,2.0,2.0) ASDEnvelope=(0.5,3.8,1.2) DriftMult=0.3 "
    "Suspension=0.99 DestBlend=Zero SrcBlend=SrcAlpha",
    "Reaction=wallskid Tex=skidwall.tga Max=128 Scale=(3.25,1.10,1.0) "
    "Pixel=NoReduceDetail Particle=Plane+Deformable+SingleSided "
    "DestBlend=InvSrcAlpha SrcBlend=SrcAlpha",
]



NEW_BLOCK_NOTE = """
// %s: AC calls this drivable, and rF2's own file only lists it among the
// barriers, which define no grip at all.
"""


def patch_tdf(template, grip, drivable=()):
    """Copy rF2's terrain file, swapping Dry/Wet grip for surfaces we use.

    Blocks are delimited by [FEEDBACK] and name their surfaces on a Materials
    line, which may list several comma separated names. Values may be numbers
    or references to names defined in [TRACKVARS], so both forms are replaced.

    Some of rF2's own blocks define no grip at all, because they describe
    things you hit rather than drive on: cem shares a line with wall and cmwl.
    AC calls its concrete IS_VALID_TRACK with a friction of 0.965, so sending
    it there left a drivable surface with no grip whatsoever -- which is what
    the concrete outside the corkscrew felt like. Any surface AC says is
    drivable gets a block of its own, taken off the barrier line first so
    there is no question which one matches.
    """
    with open(template, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    chunks = re.split(r"(?m)^(?=\[FEEDBACK\])", text)
    value = re.compile(r"(Dry|Wet)=([A-Za-z_][A-Za-z0-9_]*|[\d.]+)")
    applied = {}
    out = []
    for chunk in chunks:
        m = re.search(r"(?m)^Materials=(.+)$", chunk)
        if m:
            names = [n.strip().lower() for n in m.group(1).split(",")]
            hit = next((p for p in grip if p.lower() in names), None)
            if hit is not None:
                dry = grip[hit]

                def swap(mo, d=dry):
                    return "%s=%.3f" % (mo.group(1),
                                        d if mo.group(1) == "Dry" else d * 0.85)

                new_chunk, n = value.subn(swap, chunk)
                if n:
                    applied[hit] = dry
                    chunk = new_chunk
        out.append(chunk)

    stranded = [p for p in drivable if p not in applied and p in grip]
    if stranded:
        # Take the name off whatever gripless block claimed it.
        for i, chunk in enumerate(out):
            m = re.search(r"(?m)^Materials=(.+)$", chunk)
            if not m:
                continue
            names = [n.strip() for n in m.group(1).split(",")]
            keep = [n for n in names if n.lower() not in stranded]
            # If the stranded name was the only one there, the block really is
            # that surface's and stripping it would leave an empty line.
            if keep and len(keep) != len(names):
                out[i] = chunk.replace(m.group(0), "Materials=" + ",".join(keep))
        # Clone the road block, which is the one that describes a surface you
        # drive on, and give each stranded surface its own.
        road = next((c for c in out
                     if re.search(r"(?m)^Materials=road\s*$", c)), None)
        for prefix in stranded:
            if road is None:
                break
            block = value.sub(
                lambda mo, d=grip[prefix]: "%s=%.3f" % (
                    mo.group(1), d if mo.group(1) == "Dry" else d * 0.85), road)
            block = re.sub(r"(?m)^Materials=road\s*$",
                           "Materials=" + prefix, block)
            out.append(NEW_BLOCK_NOTE % prefix + block)
            applied[prefix] = grip[prefix]
    return "".join(out), applied



def texture_filenames(names):
    """Map each AC texture name to a unique, safe filename on disk.

    Sanitising alone is not enough: Laguna Seca ships both
    "laguna_seca_terrain_4k.dds" and "laguna-seca-terrain-4k.dds", which
    collapse onto the same name and silently overwrite each other.
    """
    out, taken = {}, set()
    for name in names:
        base, ext = os.path.splitext(name)
        cand = safe(base) + ext
        if cand.lower() in taken:
            i = 2
            while ("%s_%d%s" % (safe(base), i, ext)).lower() in taken:
                i += 1
            cand = "%s_%d%s" % (safe(base), i, ext)
        taken.add(cand.lower())
        out[name] = cand
    return out



# The level is not always a suffix. Laguna's trunks are trunk3d_DHI_001,
# trunk3d_DMID_001, trunk3d_DLOW_001 -- the level sits in the middle, between
# the group letter and the instance number. Anchoring this at the end of the
# name caught trunk3d_B_HI003 and missed every one of those, which left three
# trunks stacked in the same place.
LOD_TOKEN = re.compile(r"(?i)(LOD-FULL|LOD-FAR|MID|HI|LOW)")


def mesh_centre(node):
    """Rough middle of a mesh, for telling detail levels from separate objects."""
    if not getattr(node, "verts", None):
        return None
    n = len(node.verts)
    return [sum(v[i] for v in node.verts) / n for i in range(3)]


def world_points(node, samples=300):
    """A sample of this mesh's triangle centroids, in world space.

    AC stores a node's matrix for row vectors with the translation in the last
    row, and a node inherits its parents', so the product runs child first.
    Getting that backwards leaves every instanced prop sitting at the origin,
    where all 66 pit lane cones look perfectly coplanar with each other and
    the whole measurement is worthless.
    """
    idx, verts = node.indices, node.verts
    total = (node.index_count or 0) // 3
    if not total or not verts or not idx:
        return None
    m = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
         [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
    n = node
    while n is not None:
        if n.matrix and len(n.matrix) == 16:
            b = [n.matrix[i * 4:(i + 1) * 4] for i in range(4)]
            m = [[sum(m[r][k] * b[k][c] for k in range(4))
                  for c in range(4)] for r in range(4)]
        n = n.parent
    step = max(1, total // samples)
    out = []
    for t in range(0, total, step):
        try:
            p0, p1, p2 = (verts[idx[t * 3]], verts[idx[t * 3 + 1]],
                          verts[idx[t * 3 + 2]])
        except (IndexError, ValueError):
            continue
        cx = (p0[0] + p1[0] + p2[0]) / 3.0
        cy = (p0[1] + p1[1] + p2[1]) / 3.0
        cz = (p0[2] + p1[2] + p2[2]) / 3.0
        out.append((cx * m[0][0] + cy * m[1][0] + cz * m[2][0] + m[3][0],
                    cx * m[0][1] + cy * m[1][1] + cz * m[2][1] + m[3][1],
                    cx * m[0][2] + cy * m[1][2] + cz * m[2][2] + m[3][2]))
    return out or None


def drop_coplanar_duplicates(rows, points, gap=0.01):
    """Stop drawing a surface another copy of itself already covers.

    AC ships a low and a high detail version of each track chunk and its
    numbering does not pair them: track_low_011_SUB2 against track_hi_5_SUB1,
    same material, same ground, median height difference 0.0000 m. Both were
    being drawn, so which one won each pixel came down to floating point
    noise -- that is the flicker, and it showed up wherever two of these
    happen to overlap. No name pattern groups them, but geometry does: two
    meshes of one material lying on the same surface means one is redundant
    whatever it is called, and the thinner one stops drawing.

    Deliberately same-material only. Two different materials on one surface
    is a decal doing its job, which is what DECAL_LIFT is for.
    """
    import collections
    import numpy as np
    cell = 2.0                      # metres, the grid footprints are compared on
    # AC's own detail convention, as a token rather than a substring: "hi" on
    # its own inside track_hi_1_SUB7, but not the "hi" in whiteline.
    level_of = re.compile(r"(?i)(?:^|[_.\-])(lod-full|lod-far|hi|mid|low)(?:$|[_.\-0-9])")
    RANK = {"lod-full": 0, "hi": 0, "mid": 1, "low": 2, "lod-far": 2}

    def rank(name):
        m = level_of.search(name)
        return RANK[m.group(1).lower()] if m else None

    live = [r for r in rows
            if r.get("render", True) and not r.get("lod_of")
            and points.get(r["mesh"]) and not has_lod_range(r)]
    arr, grid, box = {}, {}, {}
    for row in live:
        p_ = np.asarray(points[row["mesh"]], dtype=float)
        arr[row["mesh"]] = p_
        grid[row["mesh"]] = set(zip((p_[:, 0] // cell).astype(int).tolist(),
                                    (p_[:, 2] // cell).astype(int).tolist()))
        box[row["mesh"]] = (p_[:, 0].min(), p_[:, 0].max(),
                            p_[:, 2].min(), p_[:, 2].max())
    fine = [r for r in live if rank(r["mesh"]) == 0]
    coarse = [r for r in live if (rank(r["mesh"]) or 0) > 0]
    dropped = 0
    # Across every material, not within one. AC splits its materials
    # differently between the two detail sets: one low chunk runs over
    # asphalt, gravel and curb while the high chunks are cut up per material,
    # so a low sheet is only ever part covered by the high meshes that happen
    # to share its material, and matching per material left two thirds of
    # them drawing. What matters is whether the high detail surface already
    # occupies that ground at that height, whatever it paints it with.
    for b_row in sorted(coarse, key=lambda r: -(rank(r["mesh"]) or 0)):
        pb = arr[b_row["mesh"]]
        bx0, bx1, bz0, bz1 = box[b_row["mesh"]]
        over = [r for r in fine
                if not (box[r["mesh"]][1] < bx0 or box[r["mesh"]][0] > bx1
                        or box[r["mesh"]][3] < bz0 or box[r["mesh"]][2] > bz1)]
        if not over:
            continue
        union = set()
        for r in over:
            union |= grid[r["mesh"]]
        bi = (pb[:, 0] // cell).astype(int)
        bk = (pb[:, 2] // cell).astype(int)
        inside = np.fromiter(
            ((int(x), int(z)) in union for x, z in zip(bi, bk)),
            dtype=bool, count=len(pb))
        # Covered ground, not a shared edge: AC's neighbouring chunks are
        # coplanar along their seam and must survive.
        if inside.mean() < 0.7:
            continue
        sel = pb[inside]
        pool = np.concatenate([arr[r["mesh"]] for r in over])
        dx = sel[:, 0][:, None] - pool[:, 0][None, :]
        dz = sel[:, 2][:, None] - pool[:, 2][None, :]
        d2 = dx * dx + dz * dz
        k = d2.argmin(axis=1)
        near = d2[np.arange(len(sel)), k] < (cell * cell)
        if int(near.sum()) < 8:
            continue
        dy = np.abs(sel[near, 1] - pool[k[near], 1])
        # Only a surface that truly shares the plane flickers. The jersey
        # barrier's far copy sits 3.5 cm off its near one and the curbs 1.3 to
        # 5 cm: wasteful to draw twice, but they do not fight for pixels, so
        # they are left alone rather than guessed at.
        if float(np.median(dy)) < gap:
            b_row["render"] = False
            b_row["lod_of"] = over[0]["mesh"]
            dropped += 1
    if dropped:
        print("   coplanar duplicates: stopped drawing %d surfaces already "
              "covered by a richer copy of themselves" % dropped)
    return dropped


def uv_samples(node, out, per_mesh=200):
    """Triangle-centre UVs for this mesh, wrapped into the texture.

    A building's white wall can be a small corner of a large atlas, so the
    atlas as a whole averages dark and the wall was never corrected. Sampling
    where the triangles actually point at gives the albedo that is drawn.
    """
    idx, uvs = node.indices, node.uvs
    total = (node.index_count or 0) // 3
    if not total or not uvs or not idx:
        return
    step = max(1, total // per_mesh)
    pts = []
    for t in range(0, total, step):
        try:
            a, b, c = uvs[idx[t * 3]], uvs[idx[t * 3 + 1]], uvs[idx[t * 3 + 2]]
        except (IndexError, ValueError, TypeError):
            continue
        u = (a[0] + b[0] + c[0]) / 3.0
        v = (a[1] + b[1] + c[1]) / 3.0
        pts.append([round(u % 1.0, 4), round(v % 1.0, 4)])
    out[node.name] = pts


def surfaces_under_rubber(rows, points, reach=1.5, height=0.3, share=0.08):
    """Materials the racing line rubber lies on, found by asking the rubber.

    "The road underneath the tire marks" is the right definition of the road,
    and the visual meshes carry no surface key to say so. Every sampled point
    of the groove overlays votes for the surface directly beneath it -- the
    nearest point within 1.5 m across and 30 cm of height -- and a material
    that collects a real share of the votes is road.

    Asking from the surfaces' side instead does not work: the groove is long
    and thin, so its samples sit metres apart and a one metre cell rarely
    holds one; widening the cell takes in the barriers and gravel alongside,
    and without the height check a bridge overhead counts as road.
    """
    import collections
    grid = collections.defaultdict(list)
    for r in rows:
        if not r.get("render", True) or r.get("lod_of") or r.get("decal"):
            continue
        if "groove" in r["ac_material"].lower():
            continue
        for x, y, z in points.get(r["mesh"]) or ():
            grid[(int(x // reach), int(z // reach))].append((x, y, z, r["ac_material"]))
    votes = collections.Counter()
    cast = 0
    for r in rows:
        if "groove" not in r["ac_material"].lower() or not r.get("render", True)                 or r.get("lod_of"):
            continue
        for x, y, z in points.get(r["mesh"]) or ():
            gx, gz = int(x // reach), int(z // reach)
            best = None
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for px, py, pz, mat in grid.get((gx + dx, gz + dz), ()):
                        if abs(py - y) > height:
                            continue
                        d2 = (px - x) ** 2 + (pz - z) ** 2
                        if d2 <= reach * reach and (best is None or d2 < best[0]):
                            best = (d2, mat)
            if best:
                votes[best[1]] += 1
                cast += 1
    if not cast:
        return set()
    return {m for m, n in votes.items() if n / float(cast) >= share}


def csp_config_paths(track_dir, ac_root):
    """The CSP config files that exist for this track."""
    track = os.path.basename(os.path.abspath(track_dir).rstrip("\/"))
    paths = [os.path.join(ac_root, "extension", "config", "tracks", "loaded", track + ".ini"),
             os.path.join(ac_root, "extension", "config", "tracks", track + ".ini"),
             os.path.join(track_dir, "extension", "ext_config.ini")]
    return [p for p in paths if os.path.exists(p)]


def csp_grass_fx(track_dir, ac_root):
    """The ground materials CSP grows GrassFX on, as patterns.

    In AC with CSP the grass the user sees on these is GrassFX's blades,
    shaded and self-shadowed, not the flat ground texture under them, and
    rF2 has nothing like it.
    """
    pats, inside = [], False
    for path in csp_config_paths(track_dir, ac_root):
        for raw in open(path, encoding="utf-8", errors="replace"):
            line = raw.split(";")[0].strip()
            if line.startswith("["):
                inside = line.upper() == "[GRASS_FX]"
                continue
            if inside and "=" in line:
                k, v = line.split("=", 1)
                if k.strip().upper() == "GRASS_MATERIALS":
                    pats += [x.strip() for x in v.split(",") if x.strip()]
    return [re.compile("^" + re.escape(x).replace("\?", ".*") + "$", re.I) for x in pats]


# July 1st: where a race is normally set, and what SEASON_SUMMER means here.
RACE_YEAR_PROGRESS = 0.5


def lut_at(text, x):
    """A CSP LUT like (|0=0|0.14=0.7|1=0|) read at x, linearly."""
    pts = []
    for part in text.strip().strip("()").split("|"):
        if "=" in part:
            a, b = part.split("=", 1)
            try:
                pts.append((float(a), float(b)))
            except ValueError:
                return None
    if not pts:
        return None
    pts.sort()
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * ((x - x0) / (x1 - x0) if x1 > x0 else 1.0)
    return pts[-1][1]


def csp_adjustments(track_dir, ac_root):
    """CSP's own material overrides for this track, as the user sees AC.

    Custom Shaders Patch ships a config per track that rewrites material
    coefficients at runtime, so the AC people actually compare against is not
    the AC in the kn5. Laguna's darkens every piece of vegetation in summer --
    BBgrass from 0.25 to 0.20, the canopies and trunks to 0.10 -- which is why
    our grass and trees stood out bright against the ground.

    Returns {material: {"ksDiffuse": v, "ksAmbient": v}} from every
    MATERIAL_ADJUSTMENT that is unconditional or tied to SEASON_SUMMER (the
    season a race is normally set in). Night and effect conditions are left
    out. CSP's wildcard "?" stands for any run of characters.
    """
    blocks, conds, cur = [], {}, None
    for path in csp_config_paths(track_dir, ac_root):
        for raw in open(path, encoding="utf-8", errors="replace"):
            line = raw.split(";")[0].strip()
            if not line:
                continue
            if line.startswith("["):
                head = line.upper()
                cur = {} if head.startswith(("[MATERIAL_ADJUSTMENT", "[CONDITION_")) else None
                if cur is not None:
                    (blocks if head.startswith("[MATERIAL") else conds.setdefault(path, [])).append(cur)
                continue
            if cur is not None and "=" in line:
                k, v = line.split("=", 1)
                cur[k.strip().upper()] = v.strip()
    # How far into each condition a midsummer race sits. A seasonal config
    # (Spa's) does not switch summer on: SEASON_SUMMER is a curve over the
    # year that tops out at 0.7 and sits near 0.5 all summer, and CSP blends
    # OFF_VALUE towards VALUE by it. Taking VALUE outright gave Spa's bbgrass
    # ksDiffuse 0.0 where AC shows it 0.10, and the grass lit by sky alone.
    weight = {}
    for cs in conds.values():
        for c in cs:
            name = c.get("NAME", "").strip().upper()
            if c.get("INPUT", "").strip().upper() == "YEAR_PROGRESS" and c.get("LUT"):
                w = lut_at(c["LUT"], RACE_YEAR_PROGRESS)
                if w is not None:
                    weight[name] = w
    out = {}
    for b in blocks:
        if b.get("ACTIVE", "1").strip() == "0":
            continue
        cond = b.get("CONDITION", "").strip().upper()
        if cond and cond != "SEASON_SUMMER" and cond not in weight:
            continue
        if cond in weight and not cond.startswith("SEASON_"):
            continue
        w = weight.get(cond, 1.0) if cond else 1.0
        pats = [x.strip() for x in b.get("MATERIALS", "").split(",") if x.strip()]
        if not pats:
            continue
        rx = [re.compile("^" + re.escape(x).replace("\?", ".*") + "$", re.I) for x in pats]
        for k, key in b.items():
            if not k.startswith("KEY_"):
                continue
            if key not in ("ksDiffuse", "ksAmbient"):
                continue
            try:
                val = float(b.get("VALUE_" + k[4:], "").split(",")[0])
                off = b.get("OFF_VALUE_" + k[4:])
                if cond and off is not None:
                    off = float(off.split(",")[0])
                    val = off + (val - off) * w
            except ValueError:
                continue
            out.setdefault(tuple(x.pattern for x in rx), {})[key] = val
    return [(tuple(re.compile(p, re.I) for p in pats), vals) for pats, vals in out.items()]


def drop_far_lods(rows):
    """Remove the levels AC only shows in the distance, and draw the rest always.

    Carrying AC's lod_in/lod_out across stopped the flicker but traded it for
    pop-in, because AC fades between levels and rF2 switches hard. AC's own
    numbers are brutal without a fade: the tree trunks are culled at 15 m, the
    canopies at 30, the tyre walls at 100, the marshals at 150 and the grass at
    200, so everything visibly snaps in as you drive at it.

    A mesh with lod_in > 0 only ever appears beyond that distance, which makes
    it the far stand-in for something already drawn from zero. Dropping those
    and giving everything else no range at all means one set of geometry, drawn
    at every distance: no switch to pop and no second copy to fight over
    pixels. Laguna's eleven green billboard panels go with them, since they
    are the only thing their material draws.
    """
    dropped = 0
    primary = {}
    for row in rows:
        if not row.get("render", True) or row.get("lod_of"):
            continue
        if (row.get("lod_in") or 0.0) == 0.0:
            primary.setdefault(row["ac_material"], 0)
            primary[row["ac_material"]] += 1
    for row in rows:
        if not row.get("render", True) or row.get("lod_of"):
            continue
        if (row.get("lod_in") or 0.0) <= 0.0:
            continue
        row["render"] = False
        row["lod_of"] = "(far detail level)"
        dropped += 1
    # Whatever survives is drawn at every distance.
    for row in rows:
        if row.get("render", True) and not row.get("lod_of"):
            row["lod_in"] = 0.0
            row["lod_out"] = 0.0
    return dropped


def has_lod_range(row):
    """Does AC give this mesh a distance range of its own?

    If it does, rF2 can switch between the detail levels exactly as AC does
    and every level should be kept: dropping them loses the far geometry, and
    drawing them all at once is what made them fight. Only meshes AC leaves
    unbounded still need one of each picking.
    """
    return (row.get("lod_out") or 0.0) > 0.0


def drop_extra_lods(rows):
    """AC ships several detail levels of an object and picks one by distance.

    Drawing them all puts identical geometry in the same place: Laguna's trees
    arrive as trunk3d_B_HI and trunk3d_B_LOW six centimetres apart, and B3HI,
    B3MID and B3LOW all on the same spot. Coincident surfaces fight over every
    pixel, which is the low detail billboard flickering against the full tree.

    Keep the richest level of each group and stop drawing the rest. Grouped by
    the name with its detail suffix removed, and only where the meshes really
    do sit on top of each other, so two genuinely separate objects that happen
    to be named alike are left alone.

    This is now only the fallback for meshes AC leaves unbounded. Anything
    carrying a lod_in/lod_out pair keeps all of its levels and is separated by
    distance instead, which is both faithful and what stops the flicker.
    """
    groups = defaultdict(list)
    for row in rows:
        m = LOD_TOKEN.search(row["mesh"])
        if not m or row.get("centre") is None:
            continue
        # Where AC states a distance range, rF2 can switch exactly as AC does
        # and every level is worth keeping: deleting the far ones throws away
        # the geometry that should appear in the distance, and it was only
        # drawing them all at once that made them fight.
        if has_lod_range(row):
            continue
        # Blank the level out wherever it sits, so every level of one object
        # collapses onto the same stem.
        stem = row["mesh"][:m.start()] + "<level>" + row["mesh"][m.end():]
        groups[stem].append(row)
    dropped = 0

    for stem, members in groups.items():
        if len(members) < 2:
            continue
        best = max(members, key=lambda r: r["tris"])
        bc = best["centre"]
        for row in members:
            if row is best or not row.get("render", True):
                continue
            c = row["centre"]
            span = max(best.get("radius", 0.0), 1.0)
            if sum((a - b) ** 2 for a, b in zip(c, bc)) ** 0.5 <= span:
                row["render"] = False
                row["lod_of"] = best["mesh"]
                dropped += 1
    return dropped


def layout_models(track_dir, layout=None):
    """The models a track or layout loads, with where models.ini puts each.

    Returns (list of (kn5 path, placement or None), layout root holding data/
    and ai/, warnings). A placement is (position, rotation) as kn5.KN5 takes
    it. A model listed twice is loaded once -- Highlands' drift layout lists
    9.kn5 twice, which would draw everything in it twice and z-fight.
    """
    folder = os.path.basename(os.path.normpath(os.path.abspath(track_dir)))
    if layout:
        ini = os.path.join(track_dir, "models_%s.ini" % layout)
        root = os.path.join(track_dir, layout)
    else:
        ini = os.path.join(track_dir, "models.ini")
        root = track_dir
    models = read_ini(ini)
    out, warn, seen = [], [], set()
    for sec in models.sections():
        if not sec.startswith("MODEL_"):
            continue
        f = (models[sec].get("FILE") or "").strip()
        if not f:
            continue
        if f.lower() in seen:
            warn.append("%s listed more than once, loaded once" % f)
            continue
        seen.add(f.lower())

        def vec(key):
            raw = (models[sec].get(key) or "0,0,0").replace(" ", "").split(",")
            v = [float(x) if x else 0.0 for x in raw[:3]]
            return v + [0.0] * (3 - len(v))
        pos, rot = vec("POSITION"), vec("ROTATION")
        place = None
        if any(pos) or any(rot):
            place = (pos, rot)
            if rot[1] or rot[2]:
                # Only the turn about the vertical has a stock model to
                # measure the convention on (kn5.placement_matrix).
                warn.append("%s ROTATION=%s: only the first number (heading) is "
                            "applied, the other two are not" % (f, rot))
        path = os.path.join(track_dir, f)
        if os.path.exists(path):
            out.append((path, place))
    if not out and os.path.exists(os.path.join(track_dir, folder + ".kn5")):
        out = [(os.path.join(track_dir, folder + ".kn5"), None)]
    return out, root, warn


def layout_files(track_dir, layout=None):
    """layout_models without the placements: (kn5 paths, layout root, warnings)."""
    models, root, warn = layout_models(track_dir, layout)
    return [p for p, _ in models], root, warn


def placements(track_dir, layout=None):
    """{kn5 path: placement} for the models models.ini moves, for kn5.KN5."""
    return {p: pl for p, pl in layout_models(track_dir, layout)[0] if pl}


def lighting(track_dir):
    """AC's own sun for this track, in degrees above the horizon and around.

    Authored per track: Laguna asks for 30 degrees, drift for 35 at a heading
    of 45. Ours sat at 72, nearly overhead, which lights a flat road and a
    banked gravel trap almost equally -- the reason the run off read dark
    against the road when AC has it the brighter of the two, and the reason
    the hills came out flat enough to look like mud.
    """
    path = os.path.join(track_dir, "data", "lighting.ini")
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8", errors="replace"):
        key, _, value = line.partition("=")
        key = key.strip().upper()
        if key in ("SUN_PITCH_ANGLE", "SUN_HEADING_ANGLE"):
            try:
                out[key.lower()] = float(value.strip())
            except ValueError:
                pass
    return out


def uv_metres(node, cap=2000):
    """How many world metres one UV unit covers, along u and along v.

    Solves the triangle's UV to world Jacobian, which is the only way to see
    that a road is mapped 171 metres to a unit one way and 12 the other.
    """
    import math
    out = []
    idx = node.indices
    ntri = node.index_count // 3
    if ntri < 1:
        return out
    step = max(1, ntri // cap)
    m = world_scale(node)
    for t in range(0, ntri, step):
        i0, i1, i2 = idx[t * 3], idx[t * 3 + 1], idx[t * 3 + 2]
        try:
            p0, p1, p2 = node.verts[i0], node.verts[i1], node.verts[i2]
            (u0, v0), (u1, v1) = node.uvs[i0], node.uvs[i1]
            (u2, v2) = node.uvs[i2]
        except (IndexError, ValueError):
            continue
        e1 = [(p1[k] - p0[k]) * m for k in range(3)]
        e2 = [(p2[k] - p0[k]) * m for k in range(3)]
        du1, dv1 = u1 - u0, v1 - v0
        du2, dv2 = u2 - u0, v2 - v0
        det = du1 * dv2 - du2 * dv1
        if abs(det) < 1e-12:
            continue
        r = 1.0 / det
        lu = math.sqrt(sum(((dv2 * e1[k] - dv1 * e2[k]) * r) ** 2
                           for k in range(3)))
        lv = math.sqrt(sum(((du1 * e2[k] - du2 * e1[k]) * r) ** 2
                           for k in range(3)))
        if 1e-3 < lu < 1e5 and 1e-3 < lv < 1e5:
            out.append((lu, lv))
    return out


def world_scale(node):
    """Uniform scale this node inherits, so the metres above are real ones."""
    scale = 1.0
    n = node
    while n is not None:
        if n.matrix:
            row = n.matrix[0:3]
            s = (row[0] ** 2 + row[1] ** 2 + row[2] ** 2) ** 0.5
            if s > 1e-6:
                scale *= s
        n = n.parent
    return scale


def ac_normals_up(node, samples=400):
    """Share of the mesh's authored vertex normals within ~8 degrees of up."""
    ns = node.normals
    if not ns:
        return 0.0
    step = max(1, len(ns) // samples)
    pick = ns[::step]
    return sum(1 for n in pick if n[1] > 0.99) / float(len(pick))


def card_height(node, samples=400):
    """Median vertical extent of a mesh's triangles, in metres.

    Tells grass cards from tree cards where nothing else does: Laguna draws
    its BBgrass on ksPerPixelAT with real face normals, like a tree would.
    """
    idx, verts = node.indices, node.verts
    total = (node.index_count or 0) // 3
    if not total or not idx or not verts:
        return 0.0
    step = max(1, total // samples)
    hs = []
    for t in range(0, total, step):
        ys = [verts[idx[t * 3 + k]][1] for k in range(3)]
        hs.append(max(ys) - min(ys))
    hs.sort()
    return hs[len(hs) // 2]


def upward_fraction(node, samples=200):
    """Share of a mesh's triangles that face upwards.

    Bounding boxes are no good for this: a row of buildings scattered along a
    straight is wide and shallow, and reads as flat. Face normals do not lie.
    """
    idx = node.indices
    verts = node.verts
    total = node.index_count // 3
    if not total:
        return 0.0
    step = max(1, total // samples)
    up = seen = 0
    for t in range(0, total, step):
        a = verts[idx[t * 3]]
        b = verts[idx[t * 3 + 1]]
        c = verts[idx[t * 3 + 2]]
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        if length < 1e-9:
            continue
        seen += 1
        if abs(ny) / length > 0.8:
            up += 1
    return (float(up) / seen) if seen else 0.0



def racing_line(track_dir):
    """AC's own lap line, used to tell driving surfaces from scenery."""
    path = os.path.join(track_dir, "ai", "fast_lane.ai")
    if not os.path.exists(path) or os.path.getsize(path) < 64:
        return []
    with open(path, "rb") as f:
        d = f.read()
    ver, count, _a, _b = struct.unpack_from("<4i", d, 0)
    if ver != 7 or count <= 0 or 16 + count * 20 > len(d):
        return []
    step = max(1, count // 400)
    return [struct.unpack_from("<3f", d, 16 + i * 20)
            for i in range(0, count, step)]


def read_ini(path):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            cp.read_file(f)
    except FileNotFoundError:
        pass
    return cp


def load_surfaces(track_dir, ac_root):
    """Global AC surfaces plus the track's own overrides, keyed by KEY."""
    out = OrderedDict()
    paths = [
        os.path.join(ac_root, "system", "data", "surfaces.ini"),
        os.path.join(track_dir, "data", "surfaces.ini"),
    ]
    for path in paths:
        cp = read_ini(path)
        for sec in cp.sections():
            if not sec.startswith("SURFACE_"):
                continue
            key = cp[sec].get("KEY", "").strip()
            if not key:
                continue
            out[key] = {
                "friction": float(cp[sec].get("FRICTION", "1.0") or 1.0),
                "valid": cp[sec].get("IS_VALID_TRACK", "0").strip() == "1",
                "pitlane": cp[sec].get("IS_PITLANE", "0").strip() == "1",
            }
    return out


def surface_for_mesh(name, keys):
    """AC physics meshes are named <digits><SURFACE_KEY><suffix>.

    Returns (key, is_physical). Longest key wins so KERB beats a shorter match.
    """
    m = DIGIT_PREFIX.match(name)
    if not m:
        return None, False
    rest = m.group(2).upper()
    best = None
    for k in keys:
        if rest.startswith(k.upper()) and (best is None or len(k) > len(best)):
            best = k
    if best is not None:
        return best, True
    # AC only lists drivable surfaces in surfaces.ini, but any mesh with a
    # numeric prefix is collidable. Walls and barriers land here, so fall back
    # to the surface names rF2 itself knows about.
    for k in SURFACE_MAP:
        if rest.startswith(k) and (best is None or len(k) > len(best)):
            best = k
    return best, True


def safe(s):
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def extract_textures(kn5_paths, tex_dir, names):
    n = 0
    for path in kn5_paths:
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            d = f.read()
        p = 6
        ver = struct.unpack_from("<I", d, p)[0]
        p += 4
        if ver > 5:
            extra = struct.unpack_from("<I", d, p)[0]
            p += 4 + extra
        cnt = struct.unpack_from("<I", d, p)[0]
        p += 4
        for _ in range(cnt):
            p += 4
            ln = struct.unpack_from("<I", d, p)[0]
            p += 4
            name = d[p:p + ln].decode("utf-8", "replace")
            p += ln
            size = struct.unpack_from("<I", d, p)[0]
            p += 4
            with open(os.path.join(tex_dir, names.get(name, name)), "wb") as o:
                o.write(d[p:p + size])
            p += size
            n += 1
    return n


def needs_blend(mat, alpha):
    """Blend only where AC blends, and only if there is transparency to show.

    Alpha tested materials count too. rF2 has no separate alpha test blend
    mode: a cut-out is a transparency material with a non-zero Alpha_Reference,
    and the two go together. Dropping the transparency and setting only the
    reference stopped the alpha channel being read at all, and every tree and
    fence drew as the solid rectangle its quad really is.
    """
    if not (mat.alpha_tested or mat.blend):
        return False
    tex = mat.textures.get("txDiffuse")
    pct = alpha.get(tex) if tex else None
    if pct is None:
        return True
    return (100.0 - pct) >= MIN_TRANSPARENT_PCT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("track_dir")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--ac-root",
                    default=r"C:\Program Files (x86)\Steam\steamapps\common\assettocorsa")
    ap.add_argument("--textures", action="store_true")
    ap.add_argument("--layout", default=None,
                    help="for a multi-layout track: the part after models_ in models_<layout>.ini")
    ap.add_argument("--tdf-template",
                    default=os.path.join(
                        r"C:\Program Files (x86)\Steam\steamapps\common",
                        "rFactor 2", "ModDev", "Locations", "terrain.tdf"),
                    help="known-good terrain file to patch grip values into")
    args = ap.parse_args()

    track_dir = os.path.abspath(args.track_dir)
    track_id = os.path.basename(track_dir.rstrip("\\/"))
    if args.layout:
        track_id = "%s_%s" % (track_id, args.layout)
    kn5_paths, layout_root, warnings = layout_files(track_dir, args.layout)
    placed = placements(track_dir, args.layout)
    for w in warnings:
        print("   WARNING %s" % w)
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = args.out or os.path.join(here, "out", track_id)
    os.makedirs(out_dir, exist_ok=True)

    surfaces = load_surfaces(layout_root, args.ac_root)
    print("surface keys: %s" % ", ".join(surfaces))


    alpha = texture_alpha_stats(kn5_paths)
    tex_names = texture_filenames(sorted(alpha))

    variants = OrderedDict()
    mesh_rows = []
    unmatched = defaultdict(int)

    for path in kn5_paths:
        if not os.path.exists(path):
            print("  skip missing %s" % os.path.basename(path))
            continue
        k = KN5(path, geometry=True, placement=placed.get(path))
        print("%s: %d materials, %d textures, version %d"
              % (os.path.basename(path), len(k.materials), len(k.textures), k.version))
        for n in k.walk():
            if n.type not in (2, 3):
                continue
            mat = k.materials[n.material_id]
            key, physical = surface_for_mesh(n.name, list(surfaces.keys()))
            if physical and key is None:
                unmatched[re.sub(r"^\d+", "", n.name)] += 1
            if physical:
                prefix = SURFACE_MAP.get((key or "").upper(), DEFAULT_PREFIX)
                rf2_mat = "%s_%s" % (prefix, safe(mat.name))
            else:
                rf2_mat = safe(mat.name)
            variants[(mat.name, key)] = rf2_mat
            mesh_rows.append({
                "mesh": n.name,
                "ac_material": mat.name,
                "shader": mat.shader,
                "surface": key,
                "physical": physical,
                "rf2_material": rf2_mat,
                "tris": n.index_count // 3,
                "transparent": bool(mat.blend),
                "render": bool(n.visible and n.renderable)
                          and not n.name.upper().startswith("AC_"),
                "alpha_test": bool(mat.alpha_tested),
                # AC's per-mesh flag; the scene file makes these shadow casters.
                "cast_shadows": bool(n.cast_shadows),
                # AC's depth modes: 0 writes depth normally, anything else is a
                # decal laid onto another surface -- the racing line rubber,
                # the sand trap borders. Coplanar with what they sit on, so
                # they need lifting off it or the two fight over every pixel.
                "decal": bool(mat.depth_mode),
                # Big and see-through is a painted backdrop -- unless AC lays
                # it onto another surface. The racing line rubber follows the
                # whole lap, so its radius is as big as any tree line: Spa's
                # groove1/groove2 were filed as backdrops, geometrically cut,
                # overflowed and never exported, and Silverstone's grass
                # border went the same way. Every rubber strip on all three
                # tracks is a decal and no backdrop is.
                "backdrop": bool(needs_blend(mat, alpha) and not mat.depth_mode
                                 and (n.radius or 0.0) > BACKDROP_RADIUS),
                "needs_blend": needs_blend(mat, alpha)
                               and not ((n.radius or 0.0) > BACKDROP_RADIUS
                                        and not mat.depth_mode),
                "radius": float(getattr(n, "radius", 0.0) or 0.0),
                # AC's own switching distances, in metres. track_hi_1_SUB7
                # draws out to 500 and track_low_014_SUB1 from 450 to 1000, so
                # in AC the two coincide only across a 50 m band. We were
                # ignoring both numbers and drawing every detail level at once,
                # which is why coplanar copies fought over pixels: that is the
                # flicker. rF2 takes the same pair as LODIn/LODOut.
                "lod_in": float(getattr(n, "lod_in", 0.0) or 0.0),
                "lod_out": float(getattr(n, "lod_out", 0.0) or 0.0),
                "centre": mesh_centre(n),
                # Which way the surface faces. A grass sheet draped over the
                # terrain is not the same thing as a grass billboard, and they
                # want opposite alpha thresholds, so measure it rather than
                # reading the material name.
                "upward": round(upward_fraction(n), 3),
                # Share of AC's own vertex normals pointing straight up. AC
                # authors grass, crowds and hedges that way so they light
                # like the ground; verify_install uses it to tell that apart
                # from the exporter writing "up" for everything.
                "ac_normals_up": round(ac_normals_up(n), 3),
                "card_height": round(card_height(n), 3),
                "textures": mat.textures,
                "props": {k: v for k, v in mat.props.items()
                          if k.startswith("mult") or k in (
                              "magicMult", "ksDiffuse", "ksAmbient",
                              "ksSpecular", "ksSpecularEXP",
                              # AC's grazing angle sheen, which rF2 has fields
                              # for and we were dropping on the floor. The road
                              # asks for a 4x boost.
                              "fresnelC", "fresnelEXP", "fresnelMaxLevel",
                              "tarmacSpecularMultiplier", "ksAlphaRef",
                              "useDetail", "detailUVMultiplier")},
            })

    # How much UV a metre of track covers, per material. A texture only looks
    # sharp if enough of its texels land on each metre, and AC maps some
    # surfaces with one copy stretched over the whole circuit.
    import statistics as _stats
    spans = defaultdict(list)
    per_axis = defaultdict(list)
    flatness = defaultdict(list)
    near = defaultdict(list)
    line_pts = racing_line(layout_root)
    # Sampled in world space so the coplanar test below can tell a second copy
    # of a surface from a separate object that merely sits near it.
    want_points = {m["mesh"] for m in mesh_rows if m.get("render", True)}
    world_pts = {}
    uv_seen = {}
    mat_of_mesh = {m["mesh"]: m["ac_material"] for m in mesh_rows}
    for path in kn5_paths:
        if not os.path.exists(path):
            continue
        model = KN5(path, geometry=True, placement=placed.get(path))
        for n in model.walk():
            if n.name in want_points and n.name not in world_pts:
                world_pts[n.name] = world_points(n, samples=1500)
                uv_samples(n, uv_seen)
            if n.type not in (2, 3) or not n.uvs or not n.verts:
                continue
            us = [u for u, _v in n.uvs]
            xs = [p2[0] for p2 in n.verts]
            zs = [p2[2] for p2 in n.verts]
            du = max(us) - min(us)
            span = ((max(xs) - min(xs)) ** 2 + (max(zs) - min(zs)) ** 2) ** 0.5
            if span > 20 and du > 0.05:
                # must come from this model, not whichever was loaded last
                name = model.materials[n.material_id].name
                spans[name].append(du / span)
                per_axis[name].extend(uv_metres(n))
                flatness[name].append(upward_fraction(n))
                if line_pts:
                    # Share of the mesh that lies close to the lap line. Using
                    # the mesh centre instead would call a terrain sheet
                    # spanning the whole site a driving surface, because its
                    # middle happens to sit near the track.
                    vs = n.verts
                    step = max(1, len(vs) // 60)
                    close = total = 0
                    for vi in range(0, len(vs), step):
                        vx, vz = vs[vi][0], vs[vi][2]
                        d2 = min((vx - q[0]) ** 2 + (vz - q[2]) ** 2 for q in line_pts)
                        total += 1
                        if d2 < 25.0 ** 2:
                            close += 1
                    near[name].append(float(close) / total if total else 0.0)
    uv_per_m = {name: _stats.median(v) for name, v in spans.items() if v}
    # Metres per UV unit along each axis separately. A road is mapped with one
    # unit running the length of the straight and a fraction of a unit across
    # it, so a single figure tiles a detail texture fourteen times too coarsely
    # one way and too finely the other. Both axes, or the grain is smeared.
    axis_m = {}
    for name, pairs in per_axis.items():
        if not pairs:
            continue
        axis_m[name] = (_stats.median([u for u, _v in pairs]),
                        _stats.median([v for _u, v in pairs]))
    ground = {name: _stats.median(v) > 0.9 for name, v in flatness.items() if v}

    # Tiling a texture forces its base to one flat colour, which is fine for a
    # road but destroys terrain, where the colour itself is the content. Only
    # surfaces that lie along the racing line are treated as driving surfaces.
    line = racing_line(layout_root)
    near_line = {}
    if line:
        for name, dists in near.items():
            near_line[name] = _stats.median(dists) > 0.7
    else:
        near_line = dict(ground)
    for m in mesh_rows:
        m["uv_per_m"] = uv_per_m.get(m["ac_material"], 0.0)
        mu, mv = axis_m.get(m["ac_material"], (0.0, 0.0))
        m["m_per_u"], m["m_per_v"] = mu, mv
        # Repeating a texture only makes sense on ground-like surfaces. A
        # building facade carries unique content and must never be tiled.
        m["ground_like"] = bool(ground.get(m["ac_material"], False))
        # The median share of this material's triangles facing up, kept as a
        # number as well as the strict ground_like flag. The exporter needs to
        # tell a flat surface from an upright one: flat ground keeps rF2's
        # plain 1.0, while a wall, a grandstand or a tree trunk has its
        # reflectance aimed at rF2's albedo band.
        _f = flatness.get(m["ac_material"])
        m["flatness"] = round(_stats.median(_f), 3) if _f else 0.0
        m["track_surface"] = bool(near_line.get(m["ac_material"], False))

    extra = drop_extra_lods(mesh_rows)
    if extra:
        print("  detail levels: kept the richest of each, stopped drawing %d" % extra)
    drop_coplanar_duplicates(mesh_rows, world_pts)
    far = drop_far_lods(mesh_rows)
    if far:
        print("   far detail levels: stopped drawing %d (AC only shows these "
              "beyond a distance; rF2 switches hard and it pops)" % far)
    csp = csp_adjustments(track_dir, args.ac_root)
    if not csp_config_paths(track_dir, args.ac_root):
        # CSP fetches a track's config the first time the track is loaded in
        # AC, into extension/config/tracks/loaded. Silverstone was converted
        # before it had been loaded once, so it came out without CSP's
        # darker asphalt, grass and trees and nothing said so.
        print("   WARNING no CSP config for this track: load it once in AC with "
              "CSP enabled, then convert again, or it will not match what you see in AC")
    hit = set()
    for m in mesh_rows:
        for rxs, vals in csp:
            if any(r.match(m["ac_material"]) for r in rxs):
                for key, val in vals.items():
                    m["props"]["csp_" + key] = val
                hit.add(m["ac_material"])
    if hit:
        print("   CSP material overrides found for: %s" % ", ".join(sorted(hit)))
    gfx = csp_grass_fx(track_dir, args.ac_root)
    for m in mesh_rows:
        m["grass_fx"] = any(r.match(m["ac_material"]) for r in gfx)
    if gfx:
        print("   CSP GrassFX grows on: %s" % ", ".join(sorted(
            {m["ac_material"] for m in mesh_rows if m["grass_fx"]})))
    # The far mountains are a painted strip whose sky is clear. They used to
    # be cut by deleting transparent triangles, because the alpha test flag
    # was unknown; a triangle straddling the ridge stayed whole and its sky,
    # stored as pale blue-white [177 208 237], drew as a white band above the
    # mountains. Colour keying is known now, so the backdrop goes through the
    # cut-out path like the trees: opaque, depth writing, sorted behind
    # everything, with the silhouette cut per pixel.
    for m in mesh_rows:
        if m.get("backdrop"):
            m["alpha_test"] = True
    under = surfaces_under_rubber(mesh_rows, world_pts)
    for m in mesh_rows:
        m["under_rubber"] = m["ac_material"] in under
    if under:
        print("   surfaces under the racing line rubber: %s" % ", ".join(sorted(under)))
    by_mat_uv = {}
    for mesh, pts in uv_seen.items():
        mat = mat_of_mesh.get(mesh)
        if mat is None:
            continue
        lst = by_mat_uv.setdefault(mat, [])
        if len(lst) < 1500:
            lst.extend(pts[:1500 - len(lst)])

    phys = [m for m in mesh_rows if m["physical"]]
    ac_mats = {m["ac_material"] for m in mesh_rows}
    print("meshes: %d total, %d physical" % (len(mesh_rows), len(phys)))
    print("material variants: %d needed from %d AC materials"
          % (len(variants), len(ac_mats)))
    split = defaultdict(set)
    for (mat, key) in variants:
        split[mat].add(key)
    for mat, keys in sorted(split.items()):
        if len(keys) > 1:
            print("  split '%s' across surfaces: %s"
                  % (mat, ", ".join(str(x) for x in sorted(keys, key=str))))
    if unmatched:
        # One line, so build_track's end-of-run summary carries the names.
        top = sorted(unmatched.items(), key=lambda x: -x[1])[:10]
        print("WARNING %d physical mesh name(s) match no AC surface, driven as "
              "plain road: %s" % (len(unmatched), ", ".join(
                  "%s x%d" % (n, c) for n, c in top)))

    # Terrain definition. Rather than author one from scratch, patch rF2's own
    # file: it already defines every surface type correctly, so we only swap in
    # the grip values AC specifies for this track.
    used = OrderedDict()
    for m in phys:
        prefix = m["rf2_material"].split("_", 1)[0]
        used.setdefault(prefix, m["surface"])
    grip, drivable = {}, []
    for prefix, key in used.items():
        info = surfaces.get(key or "", {"friction": 1.0, "valid": False})
        grip[prefix] = info["friction"]
        if info.get("valid"):
            drivable.append(prefix.lower())
    tdf_text, applied = patch_tdf(args.tdf_template, grip, drivable)
    write(os.path.join(out_dir, track_id + ".tdf"), tdf_text)
    for prefix, dry in sorted(applied.items()):
        print("  terrain %-8s dry grip %.3f (from AC)" % (prefix, dry))
    missing = [p for p in grip if p not in applied]
    if missing:
        # Expected for barriers: you hit them, you do not drive on them.
        print("  no grip block for %s (not drivable in AC, so that is right)"
              % ", ".join(missing))

    # scene file, collision only on physical meshes
    scn = ["// generated from %s" % track_id,
           "MASFile=%s.mas" % track_id, "",
           "Environment", "{", "  Fog=(0.0)", "}", ""]
    for m in mesh_rows:
        scn.append("Instance=%s" % safe(m["mesh"]))
        scn.append("{")
        scn.append("  MeshFile=%s.gmt CollTarget=%s HATTarget=%s ShadowReceiver=True"
                   % (safe(m["mesh"]),
                      "True" if m["physical"] else "False",
                      "True" if m["physical"] else "False"))
        scn.append("}")
    write(os.path.join(out_dir, track_id + ".scn"), "\n".join(scn))

    write(os.path.join(out_dir, track_id + ".gdb"),
          "%s\n{\n  TrackName = %s\n  EventName = %s\n  VenueName = %s\n"
          "  TrackNameShort = %s\n  Latitude = 0.0\n  Longitude = 0.0\n"
          "  Altitude = 0\n}\n" % (track_id, track_id, track_id, track_id, track_id))

    write(os.path.join(out_dir, "material_map.json"), json.dumps({
        "track": track_id,
        "kn5": kn5_paths,
        "placements": placed,
        "meshes": mesh_rows,
        "variants": {"%s|%s" % (a, b): v for (a, b), v in variants.items()},
        "texture_files": tex_names,
        "lighting": lighting(layout_root),
        # Where on its texture each material actually samples, so brightness
        # can be judged from what is drawn rather than from the whole atlas.
        "uv_samples": by_mat_uv,
    }, indent=1))

    rep = ["%-34s %-22s %-10s %-26s %8s"
           % ("MESH", "AC MATERIAL", "SURFACE", "RF2 MATERIAL", "TRIS")]
    for m in sorted(mesh_rows, key=lambda x: (not x["physical"], x["mesh"])):
        rep.append("%-34s %-22s %-10s %-26s %8d"
                   % (m["mesh"][:34], m["ac_material"][:22], m["surface"] or "-",
                      m["rf2_material"][:26], m["tris"]))
    write(os.path.join(out_dir, "report.txt"), "\n".join(rep))

    if args.textures:
        tex_dir = os.path.join(out_dir, "textures")
        os.makedirs(tex_dir, exist_ok=True)
        print("extracted %d textures" % extract_textures(kn5_paths, tex_dir, tex_names))

    print("\nwrote -> %s" % out_dir)
    for f in sorted(os.listdir(out_dir)):
        print("   %s" % f)


if __name__ == "__main__":
    main()
