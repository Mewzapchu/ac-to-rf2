"""Flatten AC multilayer materials into one texture each.

An AC road is not a road texture. It is a nearly blank base tinted by several
detail textures, blended through the channels of a mask and tiled at their own
rates. rF2 has no equivalent shader, so binding the base alone gives the flat
grey surface you get by default.

This composes the layers the way AC would and writes a single texture per
material, which the exporter then binds in place of the base:

    out = base * 2 * ( 0.5 + SUM over channels of mask_c * (detail_c - 0.5) )

Each mask channel selects one detail layer, and where every channel is black no
layer is selected at all, so the base survives untouched -- that is how a splat
mapped terrain keeps the aerial photo under its painted roads. Detail textures
average mid grey, so the factor of two holds an unmodulated surface at its
original brightness.

A mask with nothing painted in it does not mean "no detail" but "this surface
tiles": there the layers are a plain overlay on a near blank base, and they are
what give a road its grain.

Decompression and recompression go through rF2's own copy of texconv.
"""
import os
import json
import shutil
import struct
import argparse
import subprocess

import numpy as np

TEXCONV = (r"C:\Program Files (x86)\Steam\steamapps\common\rFactor 2"
           r"\Support\Tools\MapConverter\texconv.exe")
CHANNELS = ("R", "G", "B", "A")

# Resizing or building mips, texconv filters colour weighted by alpha unless
# told otherwise. Plenty of AC textures are fully opaque surfaces stored with a
# zero alpha channel -- Laguna's sand trap is one -- and without this every
# resized copy of them comes back pure black.
SEPALPHA = "-sepalpha"

# A composed texture holds AC's albedo and nothing else. ksDiffuse used to be
# folded in here, which lit these surfaces by a different formula from every
# other material: the coefficient landed once in the texture and again through
# Ambient_Color, so their ambient came out darker by that coefficient, and the
# diffuse went through a gamma assumption we could never confirm. It belongs in
# Diffuse_Color, where rF2 applies it in light exactly as it does for the
# buildings and trees that always looked right.


def second_stage_gain(detail_mean):
    """What rF2's two stage shader does to brightness, so it can be undone.

    L2BUMPSPECMAPT0XT1 is a modulate 2x: it multiplies the stages and doubles
    the result, which is why a detail texture averaging mid grey leaves the
    surface where it was. Compensating as though it were a plain multiply
    brightened everything by very nearly two, enough to clip the sand's red
    channel over half its area and turn tan into pale yellow.

    Dividing by the doubled mean also keeps an unusually dark detail honest:
    drift's grass averages 0.23, and a plain multiply reading would have left
    that surface at half the brightness it should have.

    Per channel, because a detail carries a colour as well as a brightness and
    that colour would otherwise wash over the whole surface. The terrain's
    detail is dead grass, so every hillside came out brown whatever the mask
    said; the sand's is neutral, so it scrubbed the warmth out of AC's sand.
    Cancelling it leaves the detail its grain and lets the first stage, where
    the mask has already put the colours, decide what colour things are.

    None means there is no second stage and nothing to undo.
    """
    if detail_mean is None:
        return 1.0
    return np.maximum(2.0 * np.asarray(detail_mean, np.float32), 1e-3)

# Texels per metre we aim for on a baked surface. Where AC stretches one
# copy of a texture over the whole circuit, no texture size can reach this,
# so the UVs are repeated instead.
TARGET_DENSITY = 600.0

# A tiled surface repeats its texture many times over, so a huge texture buys
# almost nothing: the repeats already supply the granularity. Only surfaces
# mapped once, where every texel lands somewhere unique, are worth the size.
TILED_CAP = 3072


# rF2's own cut-outs all carry one threshold. Every tree, shrub, crowd and
# fence in Joesville and Loch Drummond is 191, and the reason shows in the mip
# chain: the smaller the mip, the more of a low threshold's texture gets
# through, because building a mip averages the transparent background into the
# leaves. Ours at AC's figure of 26 went from 29% of the texture passing at
# full size to 46% at 16x16 and the grass from 33% to 77%, which is the green
# panel standing where a tree should be -- and the mip the card picks changing
# with distance is the flicker. At 191 the same textures go the other way,
# 25% down to 16%, which is why their trees hold together at any range.
CUTOUT_REF = 191

# The road under the rubber, measured against the backup that was signed off.
# The scene light went from ambient 237 / sun 245 to Joesville's 126 / 220,
# and although the road's material value rose, on screen it fell from about
# 157 to 143. This puts back exactly that 10%, on the road alone: the
# buildings and walls were too bright at the brighter light and stay as they
# are.
ROAD_LIFT = 157.3 / 143.0

# groove.dds is at most 46% opaque (22% on average) and rF2 blends it over the
# road, so however dark it is the rubber can only darken the road 1.25x on
# average -- the same in the backup as now, which is why lowering its diffuse
# changed nothing visible. Opacity is the cap, so the rubber's copy carries
# twice the opacity, and its colour reduced to grey: the texture itself is
# blue ([72 79 107]), which shows as soon as it is light enough to see.
RUBBER_OPACITY = 2.0

# The scene light the balance is worked out under: finalize_track's ambient
# and the sun's share on flat ground (220 at an elevation of 30 degrees).
SCENE_AMBIENT = 126.0
SCENE_SUN_FLAT = 110.0

# The one calibration point between AC's lighting and rF2's: Laguna's road as
# AC with CSP lights it (asphalt ksDiffuse 0.42, ksAmbient 0.35), which in rF2
# is reflectance 1.0 on a texture lifted by ROAD_LIFT and was signed off. Every
# material on every track is scaled from AC's coefficients by this same pair.
# Anchoring each track to its own road instead hid that CSP draws
# Silverstone's asphalt at 0.15 / 0.25, 0.61x Laguna's on screen, so its road
# came out about 1.9x too bright against AC and everything tied to the road
# with it. blender_stage.py carries the same two numbers.
ANCHOR_KD = 0.42
ANCHOR_KA = 0.35
GRADIENT = "gradient"   # a soft edge, which no threshold can reproduce
TRANSPARENT = 0.1       # below a tenth counts as cut away
SOLID = 0.75            # at or above three quarters counts as drawn
SOFT = 0.20             # more than this in between and it is a gradient


def cutout_ref(path, tmp, ac_ref):
    """The alpha threshold for a texture, judged from the texture itself.

    Three different things arrive here wearing AC's alpha_tested flag.

    A hard mask -- a tree atlas, a chain link fence -- is nearly all either
    cut away or solid, and gets rF2's own figure. A gradient like the gravel
    border is a deliberate soft edge between two surfaces, and a threshold
    cannot express one: any figure we pick throws away everything on one side
    of it and hardens the edge we just spent a build getting right. Those
    draw whole, which is what they did in the build that was signed off. An
    alpha channel with nothing transparent in it is not a mask at all --
    wood_planks keeps a gloss figure there, uniformly a fifth -- and loses
    its test, which is what had the whole fence drawing nowhere at 128.

    Anything we cannot read keeps what it had: not reading a texture is no
    reason to change how it draws.
    """
    sz = dds_size(path)
    if sz is None:
        return ac_ref
    a = to_rgba(path, tmp, *sz)
    if a is None:
        return ac_ref
    alpha = a[..., 3]
    cut = float((alpha < TRANSPARENT).mean())
    solid = float((alpha >= SOLID).mean())
    if cut < 0.05:
        return None                     # nothing is transparent: draw it whole
    if 1.0 - cut - solid > SOFT:
        return GRADIENT                 # a soft edge: no test at all
    return CUTOUT_REF


def run(args):
    return subprocess.run(args, capture_output=True, text=True)


def dds_size(path):
    with open(path, "rb") as f:
        h = f.read(32)
    if len(h) < 20 or h[:4] != b"DDS ":
        return None
    height, width = struct.unpack_from("<II", h, 12)
    return width, height


def to_rgba(dds, tmp, w, h):
    """Decompress a DDS to a plain RGBA array at the given size."""
    name = os.path.splitext(os.path.basename(dds))[0] + ".tga"
    out = os.path.join(tmp, name)
    if os.path.exists(out):
        os.remove(out)
    run([TEXCONV, "-ft", "tga", "-f", "R8G8B8A8_UNORM",
         "-w", str(w), "-h", str(h), "-m", "1", SEPALPHA, "-y", "-o", tmp, dds])
    if not os.path.exists(out):
        return None
    return read_tga(out)


def read_tga(path):
    with open(path, "rb") as f:
        d = f.read()
    idlen = d[0]
    w, h = struct.unpack_from("<HH", d, 12)
    bpp = d[16]
    desc = d[17]
    off = 18 + idlen
    n = w * h
    if bpp == 32:
        a = np.frombuffer(d[off:off + n * 4], dtype=np.uint8).reshape(h, w, 4)
        a = a[:, :, [2, 1, 0, 3]]           # stored BGRA
    elif bpp == 24:
        a = np.frombuffer(d[off:off + n * 3], dtype=np.uint8).reshape(h, w, 3)
        a = np.dstack([a[:, :, 2], a[:, :, 1], a[:, :, 0],
                       np.full((h, w), 255, np.uint8)])
    else:
        return None
    if not desc & 0x20:                     # origin at bottom left
        a = a[::-1]
    return a.astype(np.float32) / 255.0


def write_tga(path, rgb, alpha=None):
    h, w = rgb.shape[:2]
    hdr = bytearray(18)
    hdr[2] = 2
    struct.pack_into("<HH", hdr, 12, w, h)
    hdr[16] = 24 if alpha is None else 32
    hdr[17] = 0x20 if alpha is None else 0x28
    body = np.clip(rgb[:, :, [2, 1, 0]] * 255.0, 0, 255).astype(np.uint8)
    if alpha is not None:
        a = np.clip(alpha * 255.0, 0, 255).astype(np.uint8)
        body = np.dstack([body, a])
    with open(path, "wb") as f:
        f.write(hdr)
        f.write(body.tobytes())


def tile(sample, rx, ry, w, h):
    """Resample a texture as if repeated rx by ry times across the output."""
    if sample is None:
        return None
    sh, sw = sample.shape[:2]
    u = (np.arange(w, dtype=np.float32) / w) * rx
    v = (np.arange(h, dtype=np.float32) / h) * ry
    idx = ((u % 1.0) * sw).astype(np.int32) % sw
    idy = ((v % 1.0) * sh).astype(np.int32) % sh
    return sample[np.ix_(idy, idx)]


def bake_material(mat, tex_dir, tmp, cap, target=TARGET_DENSITY, grain=1.0,
                  lowfreq=False, second_mean=None):
    names = mat["textures"]
    base_name = names.get("txDiffuse")
    mask_name = names.get("txMask")
    if not base_name:
        return None
    files = mat["_files"]

    # Two shapes of the same idea. A multilayer material picks between four
    # detail textures with a mask; a "NMDetail" one has a single detail and no
    # mask at all, which is how Laguna's sand trap gets its gravel. Without it
    # the sand is only the low frequency base, which is what made it look
    # blurred next to AC.
    multi = bool(mask_name) and not mask_name.lower().startswith("null")
    slots = ["txDetail" + c for c in CHANNELS] if multi else ["txDetail"]

    base_path = os.path.join(tex_dir, files.get(base_name, base_name))
    mask_path = (os.path.join(tex_dir, files.get(mask_name, mask_name))
                 if multi else None)
    size = dds_size(base_path)
    if size is None:
        return None
    # Keep the base texture's own shape and resolution. Forcing everything to a
    # square loses detail on big terrain maps and squashes long road strips.
    bw, bh = size

    # Whether it is worth baking above the base's own resolution depends on the
    # base. A near blank road base loses nothing by being upscaled, and the
    # extra pixels get filled with detail at its native resolution. A base that
    # carries real painted detail only blurs, so it keeps its own size.
    probe = to_rgba(base_path, tmp, 256, 256)
    flat = probe is not None and float(probe[:, :, :3].std()) * 255.0 < 18.0

    # Does the mask paint anything? A surface meant to tile carries a mask with
    # no picture in it: Laguna's is 32x32 of one flat colour, which says "this
    # layer, everywhere" and nothing about position. A uniquely mapped surface
    # has a real one. The test has to be per channel and across the image, not
    # over the mask as a whole, or a flat mask whose channels merely differ from
    # each other reads as painted. Measured across both tracks the split is
    # absolute: flat masks vary by exactly zero, painted ones by at least 0.18.
    # A painted mask means the base is a one-off map -- an aerial photo, a baked
    # shadow map -- that must keep its own place on the mesh, however smooth it
    # looks to the flatness test above.
    mprobe = to_rgba(mask_path, tmp, 256, 256) if multi else None
    const = ([float(mprobe[:, :, i].mean()) for i in range(len(CHANNELS))]
             if mprobe is not None else [])
    painted = (mprobe is not None
               and max(float(mprobe[:, :, i].std())
                       for i in range(len(CHANNELS))) > 0.01)

    # Work out up front whether this surface will end up tiled, since a tiled
    # one gets its texture repeated many times over and a huge bake buys it
    # nothing: the repeats already supply the granularity.
    # "Stretched" means AC maps one copy of this texture over the whole circuit
    # rather than repeating it, so its own resolution is all the detail there is.
    stretched = float(mat.get("uv_per_m", 0.0)) < 0.01
    prelim = float(mat.get("uv_per_m", 0.0)) * bw
    tiled = (prelim > 0.0 and flat and not painted
             and mat.get("ground_like") and stretched
             and target / prelim > 2.0)
    limit = min(cap, TILED_CAP) if tiled else cap
    scale = float(limit) / max(bw, bh)
    if painted or not flat:
        scale = min(scale, 1.0)
    w, h = max(4, int(bw * scale)), max(4, int(bh * scale))
    budget = 64_000_000
    if w * h > budget:
        shrink = (float(budget) / (w * h)) ** 0.5
        w, h = max(4, int(w * shrink)), max(4, int(h * shrink))
    upscale = float(w) / bw

    base = to_rgba(base_path, tmp, w, h)
    mask = to_rgba(mask_path, tmp, w, h) if painted else None
    if base is None or (painted and mask is None):
        return None

    props = mat.get("props", {})

    # One detail layer per mask channel, in RGBA order: AC picks the layer for a
    # pixel with that channel of the mask.
    layers = [None] * len(slots)
    for i, slot in enumerate(slots):
        det_name = names.get(slot)
        if not det_name or det_name.lower().startswith("null"):
            continue
        det_path = os.path.join(tex_dir, files.get(det_name, det_name))
        ds = dds_size(det_path)
        if ds is None:
            continue
        dw, dh = ds
        det = to_rgba(det_path, tmp, min(dw, cap), min(dh, cap))
        if det is None:
            continue
        if lowfreq:
            # rF2 is sampling the grain itself on the second stage, so this
            # bake only has to say where each layer's colour falls. Its own
            # average is exactly that, and it costs nothing to tile.
            flat_rgb = det[:, :, :3].reshape(-1, 3).mean(axis=0)
            layers[i] = np.broadcast_to(flat_rgb, (h, w, 3)).astype(np.float32)
            continue
        # Repeat the detail enough to keep its own texel density rather than
        # stretching one copy across the whole surface. Finer than this and it
        # reads as noise rather than asphalt.
        rx = max(1.0, min(8.0, round(float(bw) * upscale * grain / max(dw, 1))))
        ry = max(1.0, min(8.0, round(float(bh) * upscale * grain / max(dh, 1))))
        layers[i] = tile(det, rx, ry, w, h)[:, :, :3]
    if all(d is None for d in layers):
        return None

    def strength(i):
        """How hard AC drives this layer. Zero means unset, not silent."""
        key = "mult" + (CHANNELS[i] if multi else "")
        return min(max(float(props.get(key, 0.0)), 0.0), 1.0) or 1.0

    # How AC reads the mask: each channel selects a detail layer for that pixel,
    # and where every channel is black NO layer is selected, so the base shows
    # through untouched. Laguna's terrain mask paints only 9% of the map; the
    # other 91% is the aerial photo that carries every green and brown on the
    # hillsides, and covering it with detail layers is what lost them.
    #
    # Some masks carry no signal at all: asphalt ships a 32x32 black one. There
    # the layers are meant as a plain overlay on the base, and that overlay is
    # what gives an otherwise blank road its grain.
    if painted:
        # The mask alone decides: a channel at full selects its layer outright,
        # and where the channels sum to less than one the base shows through by
        # the remainder. AC's mult is how finely the layer tiles, not how
        # strongly it tints -- reading it as a strength left Laguna's hillsides
        # with 7% of their grass, which is the brown they came out.
        msum = np.zeros((h, w), np.float32)
        acc = np.zeros((h, w, 3), np.float32)
        for i, det in enumerate(layers):
            if det is None:
                continue
            msum += mask[:, :, i]
            acc += mask[:, :, i][:, :, None] * det
        cover = np.clip(msum, 0.0, 1.0)[:, :, None]
        detail = (1.0 - cover) * 0.5 + acc / np.maximum(msum, 1.0)[:, :, None]
    else:
        # A flat mask still says which layers to use, so its constant value is
        # the weight. Where it says nothing at all, share the layers out by
        # strength alone.
        weights = [0.0 if d is None else
                   (const[i] if multi else 1.0) * strength(i)
                   for i, d in enumerate(layers)]
        if sum(weights) <= 1e-6:
            weights = [0.0 if d is None else strength(i)
                       for i, d in enumerate(layers)]
        wsum = sum(weights)
        detail = np.zeros((h, w, 3), np.float32)
        for wgt, det in zip(weights, layers):
            if det is not None and wgt > 0.0:
                detail += det * (wgt / wsum)
    detail = np.clip(detail, 0.0, 1.0)

    # If one copy of this texture is stretched over a huge area, repeating it is
    # the only way to get detail onto the surface, and a near blank base tiles
    # without anyone seeing the repeat. A base that holds unique content -- an
    # aerial photo, a painted mask -- cannot be repeated at all: that tiles the
    # content visibly, and flattening it to its average to hide the seam throws
    # the content away. Those surfaces keep their own mapping.
    density = float(mat.get("uv_per_m", 0.0)) * w
    repeat = 1.0
    if density > 0.0 and flat and not painted and mat.get("ground_like") and stretched:
        repeat = max(1.0, min(256.0, target / density))

    # Hold the surface at the brightness AC gives it. Named apart from the
    # density target above, which a plain "target" here used to overwrite.
    magic = float(props.get("magicMult", 1.0)) or 1.0
    out = base[:, :, :3] * detail * 2.0 * (magic / second_stage_gain(second_mean))

    alpha = blend_alpha(mat, base, tex_dir, tmp, mat["_files"], w, h)
    return np.clip(out, 0.0, 1.0), repeat, alpha


def wants_bake(mat):
    """Does this material compose textures that rF2 cannot compose itself?"""
    shader = mat["shader"].lower()
    if "multilayer" in shader:
        return True
    # Only the shaders that say "detail" actually sample the detail slot. The
    # plain multimap ones leave a copy of their specular map sitting in it,
    # which is not a colour at all and would ruin the surface if multiplied in.
    if "detail" not in shader:
        return False
    det = mat["textures"].get("txDetail", "NULL")
    return (not det.lower().startswith("null")
            and det != mat["textures"].get("txMaps"))


# Grass cards drawn darker in the texture itself. Their material figures went
# from 0.41/0.24 to 0.15/0.07 at Silverstone with no visible change: rF2 lights
# mostly from its sky (image based), on the texture, and its own content keeps
# those figures at 1.0 with the albedo in the texture. So the texture is the
# lever that works. Anchored to the user's "much darker", not measured.
GRASS_CARD_TEXTURE = 0.8    # 0.5 far too dark, 0.875 a bit light (user)


def is_grass_card(row):
    """Cut-out vegetation that is a grass card rather than a tree or bush.

    Same rule as the exporter's: AC authors it with straight-up normals, or
    it is under a metre tall on an alpha test or grass shader (grass 0.1-0.8
    m, bushes 0.9-1.7, trees 2.7 and up).
    """
    if not row:
        return False
    sh = row.get("shader") or ""
    if not (sh in ("ksTree", "ksGrass") or "csp_ksDiffuse" in row.get("props", {})):
        return False
    if float(row.get("card_height", 99.0)) >= 1.0:
        return False                    # pines and hedges have up normals too
    return (float(row.get("ac_normals_up", 0.0)) > 0.5
            or "AT" in sh or sh == "ksGrass")


# Where to aim an over-bright texture, from the 342 textures measured across
# Joesville and Loch Drummond (albedo p25 51, median 78, p75 115). The same
# figures the exporter used to aim Diffuse/Ambient at -- which does next to
# nothing in rF2: it lights the texture mostly from its sky, so Wuhu Town's
# white walls stayed white at 0.34. The texture is the lever that works (as
# for the grass cards), so the darkening goes into a copy of it.
# Only what lies above their band is pulled in, to its top (p75): the middle
# of it darkened 82 of Wuhu's 94 materials and Silverstone's flags to x0.25.
SURFACE_ALBEDO = 115.0  # upright: aimed with the brightest tenth drawn
CUTOUT_ALBEDO = 115.0   # cut-outs: fences, tents, flags
GROUND_ALBEDO = 115.0   # plain flat ground: its average


def albedo_caps(drawn, lums, p90, cutouts):
    """How much to darken each over-bright material's texture, 0.15-1.

    Left alone: anything a two stage or bake composes (signed off, and the
    base is not the final albedo), the asphalt under the rubber, decals and
    markings, the backdrop, the rubber itself, and vegetation, which has its
    own handling.
    """
    out = {}
    for name, m in drawn.items():
        lum = lums.get(name)
        if not lum or m.get("two_stage") or m.get("baked") or m.get("decal") \
                or m.get("backdrop") or m.get("under_rubber") \
                or "groove" in m.get("ac_material", "").lower():
            continue
        sh = m.get("shader") or ""
        if sh in ("ksTree", "ksGrass") or (name in cutouts
                                            and "csp_ksDiffuse" in m.get("props", {})):
            continue
        flat = (float(m.get("flatness", 0.0)) > 0.5
                and bool(m.get("track_surface")))
        if flat:
            cap = GROUND_ALBEDO / lum
        elif name in cutouts:
            cap = CUTOUT_ALBEDO / lum
        else:
            cap = SURFACE_ALBEDO / max(p90.get(name) or lum, lum)
        cap = max(0.15, min(1.0, cap))
        if cap < 0.95:
            out[name] = round(cap * 20) / 20.0
    return out


def dim_copy(disk, scale, tex_dir, tmp, texconv):
    """A copy of a diffuse texture with its colour scaled, alpha untouched."""
    path = os.path.join(tex_dir, disk)
    sz = dds_size(path)
    img = to_rgba(path, tmp, sz[0], sz[1]) if sz else None
    if img is None:
        return None
    alpha = img[:, :, 3] if float(img[:, :, 3].std()) > 0.01 \
        or float(img[:, :, 3].mean()) < 0.99 else None
    stem = os.path.splitext(disk)[0] + "_d%02d" % int(round(scale * 100))
    return write_dds(stem, np.clip(img[:, :, :3] * scale, 0.0, 1.0), alpha,
                     tex_dir, tmp, texconv)


def coverage_copy(disk, ref, tex_dir, tmp, texconv, rgb_scale=1.0):
    """A cut-out texture whose alpha draws AC's shape at rF2's threshold.

    Neither threshold works on its own. AC's textures are authored for low
    ones: at 191 every fence panel loses its chain-link mesh outright (fences
    keeps 49% of what AC draws at 26), the privacy screens turn to speckle,
    and the grass blades are stripped back to their base. But passing texels
    with mid-range alpha read as see-through in rF2, which is why every one
    of its own cut-outs sits at 191, and lowering the threshold made canopies
    and bushes go transparent.

    So the alpha is remapped instead of the threshold: whatever AC draws at its
    own reference is lifted to 191 and above, and whatever AC cuts stays below.
    The test stays at 191, every texel that passes is near opaque, and the
    shape is AC's. Mips are then rebuilt holding coverage at 191, so a low
    authored threshold no longer fills the cut-out in at a distance.
    """
    path = os.path.join(tex_dir, disk)
    sz = dds_size(path)
    if not sz:
        return None
    img = to_rgba(path, tmp, sz[0], sz[1])
    if img is None:
        return None
    r = ref / 255.0
    t = CUTOUT_REF / 255.0
    a = img[:, :, 3]
    above = a >= r
    mapped = np.where(above,
                      t + (a - r) / max(1.0 - r, 1e-6) * (1.0 - t),
                      a * ((t - 1.0 / 255.0) / max(r, 1e-6)))
    stem = os.path.splitext(disk)[0] + "_a%d" % ref
    if rgb_scale != 1.0:
        stem += "_d%02d" % int(round(rgb_scale * 100))
    return write_dds(stem, np.clip(img[:, :, :3] * rgb_scale, 0.0, 1.0),
                     np.clip(mapped, 0.0, 1.0), tex_dir, tmp, texconv,
                     keep_coverage=t)


def blend_details(mat, name, probe, tex_dir, tmp, files, size, texconv):
    """A colour-neutral grain map for a painted multilayer surface.

    Where the mask paints, the first stage is baked with each layer's colour
    already in it (bake_material's low frequency pass), so the hillside's tan
    dead grass and olive grass are in the base. Tiling a coloured detail over
    that applied the layer colours a second time: on Laguna's terrain it
    dropped the blue channel from 94 to 80 and pushed the whole hillside
    browner and more saturated, which is the mud. rF2's own content does not
    do this -- its detail maps are neutral grey grain (GRASS_MULT is
    [219 222 214], GRASSVERGES_MULT [211 210 211]) and the colour lives in the
    base.

    So the layers are averaged by how much of the map the mask gives each one,
    reduced to luminance, and centred on mid grey: through rF2's modulate 2x
    that is exactly 1.0 on average, so it leaves the base's colour alone and
    adds only the grain.

    Weighted by coverage alone. AC's multX is how finely each layer tiles, not
    how strongly it tints; reading it as a weight once gave the soil 49% of
    the mix on the strength of a tiling factor, and the comment in
    bake_material records the same mistake being made and fixed before.
    """
    weights, layers = [], []
    for i, c in enumerate(CHANNELS):
        tex = mat["textures"].get("txDetail" + c, "NULL")
        if tex.lower().startswith("null"):
            continue
        w = float(probe[:, :, i].mean())
        if w <= 0.0:
            continue
        weights.append(w)
        layers.append(tex)
    if not layers:
        return None
    total = sum(weights)
    lum = None
    want = 0.0                       # the contrast the layers have on their own
    for w, tex in zip(weights, layers):
        img = to_rgba(os.path.join(tex_dir, files.get(tex, tex)), tmp, size, size)
        if img is None:
            return None
        l = img[:, :, :3].mean(axis=2)
        want += (w / total) * float(l.std() / max(l.mean(), 1e-6))
        part = l * (w / total)
        lum = part if lum is None else lum + part
    # Averaging textures pixel by pixel cancels their grain against each other:
    # dead grass alone has a contrast of 0.273 and the four-way average came
    # out at 0.123, which is the smooth, characterless surface that reads as
    # mud. Put back the contrast the layers carry on their own, weighted the
    # same way. Official ground albedo sits at 0.15 to 0.23, and Laguna's
    # layers weight out at about 0.20.
    dev = lum - float(lum.mean())
    have = float(dev.std()) / 0.5
    if have > 1e-6:
        dev *= (want / have)
    grain = np.clip(0.5 + dev, 0.0, 1.0)
    out = write_dds(name + "_detail", np.dstack([grain, grain, grain]), None,
                    tex_dir, tmp, texconv)
    if out:
        print("  %-22s neutral grain, contrast %.3f, from %s" % (name[:22], want, ", ".join(
            "%s %.0f%%" % (t, 100.0 * w / total)
            for w, t in sorted(zip(weights, layers), reverse=True))))
        nm = detail_normal(name, grain, tex_dir, tmp, texconv)
        if nm:
            mat["_detail_nm"] = nm
    return out


# How much relief a ground normal map adds, measured on Joesville's GRASS_NORM:
# a mean surface tilt of 5.1 degrees, 8.1 at the 90th percentile.
GROUND_TILT_DEG = 5.1


def detail_normal(name, height, tex_dir, tmp, texconv):
    """A tangent-space normal map from a grain texture used as a height field.

    AC's terrain carries no normal map at all, so in rF2 the hillside lit as a
    single smooth sheet. Official ground does carry one. This takes the grain
    as height, with wrap-around gradients so it tiles as seamlessly as the
    grain does, and scales the relief until its mean tilt matches Joesville's
    grass rather than picking a strength by eye.
    """
    gx = (np.roll(height, -1, axis=1) - np.roll(height, 1, axis=1)) * 0.5
    gy = (np.roll(height, -1, axis=0) - np.roll(height, 1, axis=0)) * 0.5
    mag = np.sqrt(gx * gx + gy * gy)
    target = np.tan(np.radians(GROUND_TILT_DEG))
    k = target / max(float(mag.mean()), 1e-6)
    nx, ny = -gx * k, -gy * k
    nz = np.ones_like(nx)
    ln = np.sqrt(nx * nx + ny * ny + nz * nz)
    rgb = np.dstack([nx / ln, ny / ln, nz / ln]) * 0.5 + 0.5
    tilt = np.degrees(np.arccos(np.clip(nz / ln, -1, 1)))
    out = write_dds(name + "_detail_nm", rgb, None, tex_dir, tmp, texconv)
    if out:
        print("  %-22s detail normal map, mean tilt %.1f deg" % (name[:22], float(tilt.mean())))
    return out


def single_detail(mat, tex_dir, tmp, files, name=None, size=1024, texconv=TEXCONV):
    """Name the detail rF2 should sample itself, and say if a mask paints.

    A baked copy of a detail can only hold as much grain as the bake's
    resolution allows, and where AC stretches one base over a whole circuit
    that is nowhere near enough: drift's road is a shadow map spread over the
    track, so its asphalt grain had nothing left to sit on. rF2 can sample a
    detail through its own UV layer at whatever tiling the surface needs.

    Only one detail can go on the second stage, so where a mask paints several
    the one it gives the most area to is chosen. The rest still reach the
    surface through the first stage, as the colours they average out to.

    Returns (detail name, painted) or None.
    """
    names = mat["textures"]
    mask_name = names.get("txMask")
    multi = bool(mask_name) and not mask_name.lower().startswith("null")
    if not multi:
        det = names.get("txDetail", "NULL")
        return None if det.lower().startswith("null") else (det, False)

    probe = to_rgba(os.path.join(tex_dir, files.get(mask_name, mask_name)),
                    tmp, 256, 256)
    if probe is None:
        return None
    painted = max(float(probe[:, :, i].std())
                  for i in range(len(CHANNELS))) > 0.01
    # A painted mask means the base already carries each layer's colour, so
    # the second stage has to be colour-neutral grain or the colours land
    # twice. Everything else keeps the original behaviour exactly: the single
    # layer the mask gives the most area to, which is what the road, curbs and
    # gravel were built with when they were signed off.
    # Off the racing surface only. The curbs, gravel and asphalt variants are
    # painted too and carry the same double colour in principle, but they
    # were signed off as they are, and a change nobody asked for to surfaces
    # that work is not one to make alongside a fix for the hills.
    if painted and name is not None and not mat.get("track_surface"):
        grain = blend_details(mat, name, probe, tex_dir, tmp, files, size, texconv)
        if grain:
            return grain, painted
    for i in sorted(range(len(CHANNELS)),
                    key=lambda c: -float(probe[:, :, c].mean())):
        det = names.get("txDetail" + CHANNELS[i], "NULL")
        if not det.lower().startswith("null"):
            return det, painted
    return None


def ac_detail_tiling(mat, det, files):
    """How many times AC repeats this detail per unit of the mesh's UVs.

    AC samples a detail at the mesh UV times its multiplier: multR..multA on
    ksMultilayer, one per layer, and detailUVMultiplier on the NMDetail
    shaders. CSP's materials_pbr.ini spells it out (DetailNMMult "UV
    multiplier", and a parallax variant chosen by whether MultR..A are equal,
    which only matters if they are UV scales), and values like 150 or 4200
    on Spa's buildings only make sense as tiling.

    The second stage used to tile every detail every 2 m instead. Silverstone
    authors its asphalt detail to span 44 x 132 m (multR 1.0) and Spa's 13 x
    95 m; packed into 2 m squares, blotches meant to vary over a straight
    became a pattern that visibly repeats at speed. Highlands' road, which AC
    already repeats every 0.8 x 6 m, was the one that looked right.

    The highest multiplier among the layers that carry this texture, since
    a layer at 0 is AC sampling one texel: no grain from it at all. All zero
    means the detail is a flat colour in AC, so it is spread out until it is
    one here too. None for a grain blended here, which AC has no figure for.
    """
    props = mat.get("props", {})
    names = mat["textures"]
    det_l = det.lower()
    if names.get("txMask", "NULL").lower().startswith("null"):
        if names.get("txDetail", "").lower() != det_l and \
                files.get(names.get("txDetail", ""), "").lower() != det_l:
            return None
        f = props.get("detailUVMultiplier")
        return None if f is None else max(0.001, min(4096.0, float(f)))
    mults = [float(props.get("mult" + c, 0.0)) for c in CHANNELS
             if det_l in (names.get("txDetail" + c, "").lower(),
                          files.get(names.get("txDetail" + c, ""), "").lower())]
    if not mults:
        return None
    return max(0.001, min(4096.0, max(mults)))


# The coarsest a second stage detail is allowed to get: 128 of its texels to
# the metre, about 8 mm each, so a 1024 detail repeats at most every 8 m and
# a 2048 one every 16 m. Past that the grain reads as blur.
DETAIL_TEXELS_PER_M = 128.0
# Ship driving-surface details one mip down. Tried 2026-10-11; no effect on
# the near band, whose cause was the detail normal map's tiling.
HALVE_ROAD_DETAIL = False
# Relative contrast (std / mean of luminance) of the driving-surface details
# that read right in the sim: Highlands' Tarmac_5D_A 0.34, Laguna's
# tarmac_detail_B 0.31. Silverstone's and Spa's sit at 0.52-0.53.
DETAIL_MAX_CONTRAST = 0.34
# Relative contrast the asphalt detail holds through mip 3, then x0.75 per
# mip: between the good pair at full size and what their mips keep further out.
EVEN_DETAIL_CONTRAST = 0.28
EVEN_DETAIL_HOLD = 3
# Off: holding contrast into the far mips made the band more visible (user,
# 2026-10-12). The asphalt is softened evenly and tiled denser instead.
EVEN_MIPS = False
# How often the asphalt under the rubber repeats its detail (Highlands' 0.5 m).
ROAD_DETAIL_METRES = 0.5
# Asphalt under the rubber only: applied to every track surface it had also
# flattened the signed-off terrain grass. AC's exact tiling, the alternative,
# stretched the asphalt (2026-10-12).
SOFTEN_DETAIL = True


def repeat_metres(mat, det, files, tex_dir, floor):
    """How far apart the second stage detail repeats on the ground, square.

    2 m everywhere was the signed-off look but packed blotches meant to vary
    over a straight into a visible pattern. AC's own repeat (ac_detail_tiling)
    is used to let a detail grow coarser than that: its shorter axis, since
    AC's mapping is anisotropic and the longer one would smear the grain.
    Never finer than `floor`, so nothing gets busier than the build that
    worked, and never coarser than DETAIL_TEXELS_PER_M allows.
    """
    f = ac_detail_tiling(mat, det, files)
    spans = [s for s in (float(mat.get("m_per_u", 0.0)),
                         float(mat.get("m_per_v", 0.0))) if s > 0]
    if f is None or not spans:
        return floor
    ac = min(spans) / f
    sz = dds_size(os.path.join(tex_dir, files.get(det, det)))
    cap = (max(sz) / DETAIL_TEXELS_PER_M) if sz else floor
    return max(floor, min(ac, max(cap, floor)))


def even_mip_detail(rgb, alpha, stem, tex_dir, tmp, texconv):
    """Write a detail whose mips all carry a planned share of contrast.

    Each level is the box-filtered one below it, then stretched about its
    mean to EVEN_DETAIL_CONTRAST (held to mip EVEN_DETAIL_HOLD, fading after).
    texconv compresses each level alone; their blocks go into one DDS.
    Returns (name, contrast before, contrast after) or None.
    """
    def rel(x):
        l = x.mean(axis=2)
        return float(l.std() / max(l.mean(), 1e-3))

    levels, alphas, lvl, a = [], [], rgb.astype(np.float32), alpha
    while True:
        levels.append(lvl)
        alphas.append(a)
        h, w = lvl.shape[:2]
        if h == 1 and w == 1:
            break
        h2, w2 = max(1, h // 2), max(1, w // 2)
        lvl = lvl[:h2 * (h // h2), :w2 * (w // w2)].reshape(
            h2, h // h2, w2, w // w2, 3).mean(axis=(1, 3))
        if a is not None:
            a = a[:h2 * (h // h2), :w2 * (w // w2)].reshape(
                h2, h // h2, w2, w // w2).mean(axis=(1, 3))
    before, after, blocks, header = [], [], [], None
    fmt = "BC1_UNORM" if alpha is None else "BC3_UNORM"
    for k, lvl in enumerate(levels):
        mean = lvl.reshape(-1, 3).mean(axis=0)
        r = rel(lvl)
        want = EVEN_DETAIL_CONTRAST * (0.75 ** max(0, k - EVEN_DETAIL_HOLD))
        out = np.clip(mean + (lvl - mean) * (want / r if r > 1e-4 else 1.0),
                      0.0, 1.0)
        before.append(r)
        after.append(rel(out))
        part = "%s_m%d" % (stem, k)
        write_tga(os.path.join(tmp, part + ".tga"), out, alphas[k])
        run([texconv, "-f", fmt, "-m", "1", SEPALPHA, "-y", "-o", tmp,
             os.path.join(tmp, part + ".tga")])
        made = [f for f in os.listdir(tmp)
                if f.lower() == (part + ".dds").lower()]
        if not made:
            return None
        data = open(os.path.join(tmp, made[0]), "rb").read()
        if data[84:88] == b"DX10":
            return None
        header = header or bytearray(data[:128])
        blocks.append(data[128:])
    # Mip count and the flags that say there is a chain.
    struct.pack_into("<I", header, 28, len(levels))
    struct.pack_into("<I", header, 8, struct.unpack_from("<I", header, 8)[0] | 0x20000)
    struct.pack_into("<I", header, 108,
                     struct.unpack_from("<I", header, 108)[0] | 0x400008)
    with open(os.path.join(tex_dir, stem + ".dds"), "wb") as f:
        f.write(bytes(header))
        for b in blocks:
            f.write(b)
    show = lambda l: " ".join("%.2f" % x for x in l[:6])
    return stem + ".dds", show(before), show(after)


def road_spec_map(mat, tex_dir, tmp, files, texconv, cap=2048):
    """AC's specular mask for a surface without txMaps: txDiffuse's alpha."""
    base_name = mat["textures"].get("txDiffuse")
    if not base_name:
        return None
    path = os.path.join(tex_dir, files.get(base_name, base_name))
    size = dds_size(path)
    if size is None:
        return None
    scale = min(1.0, float(cap) / max(size))
    img = to_rgba(path, tmp, max(4, int(size[0] * scale)), max(4, int(size[1] * scale)))
    if img is None or img.shape[2] < 4 or float(img[:, :, 3].std()) < 0.01:
        return None
    a = img[:, :, 3:4]
    return write_dds(mat["rf2_material"] + "_spec", np.repeat(a, 3, axis=2),
                     None, tex_dir, tmp, texconv)


def write_dds(stem, rgb, alpha, tex_dir, tmp, texconv, keep_coverage=None):
    """Compress one composed texture and return the name it went out as.

    keep_coverage, a 0..1 alpha test reference, rebuilds the mip chain so the
    share of texels passing that threshold stays what it is at full size.
    """
    tga = os.path.join(tmp, stem + ".tga")
    write_tga(tga, rgb, alpha)
    extra = ["-keepcoverage", "%.4f" % keep_coverage] if keep_coverage else []
    run([texconv, "-f", "BC1_UNORM" if alpha is None else "BC3_UNORM",
         "-m", "0", SEPALPHA] + extra + ["-y", "-o", tex_dir, tga])
    made = os.path.join(tex_dir, stem + ".DDS")
    final = os.path.join(tex_dir, stem + ".dds")
    if os.path.exists(made) and made != final:
        os.replace(made, final)
    return (stem + ".dds") if os.path.exists(final) else None


def blend_alpha(mat, base, tex_dir, tmp, files, w, h):
    """The alpha that fades this surface into its neighbour, wherever AC put it.

    A material that blends needs a shape in its alpha or it draws as a hard
    on/off edge. Several AC materials keep a flat value in the diffuse and the
    real gradient somewhere else: sand_trap_border, which is precisely the
    material whose job is to feather sand into gravel, carries 0.004 flat in
    its diffuse and a full range gradient in the alpha of its normal map.
    Take whichever map actually has a shape in it.
    """
    if not (mat.get("transparent") or mat.get("alpha_test")
            or mat.get("needs_blend")):
        return None
    if base is not None and base.shape[2] > 3 and float(base[:, :, 3].std()) > 0.01:
        return base[:, :, 3]
    names = mat["textures"]
    for slot in ("txNormal", "txDetail", "txDetailNM", "txNormalDetail"):
        t = names.get(slot, "NULL")
        if t.lower().startswith("null"):
            continue
        a = to_rgba(os.path.join(tex_dir, files.get(t, t)), tmp, w, h)
        if a is not None and a.shape[2] > 3 and float(a[:, :, 3].std()) > 0.01:
            return a[:, :, 3]
    return None


def detail_mean(det_name, tex_dir, tmp, files):
    """A detail's average colour, which the first stage divides out."""
    grey = np.full(3, 0.5, np.float32)
    path = os.path.join(tex_dir, files.get(det_name, det_name))
    size = dds_size(path)
    if size is None:
        return grey
    a = to_rgba(path, tmp, min(size[0], 256), min(size[1], 256))
    return grey if a is None else a[:, :, :3].reshape(-1, 3).mean(axis=0)


def write_base(mat, det_name, tex_dir, tmp, cap, files, texconv):
    """Write the base for a two stage material, scaled to survive the multiply.

    rF2 multiplies the two stages together and a detail texture averages mid
    grey, so the product comes out around half the brightness of the base. The
    base is pre-scaled by the detail's own mean to put it back, which also
    folds in AC's diffuse coefficient the way a bake would.
    """
    names = mat["textures"]
    base_name = names["txDiffuse"]
    base_path = os.path.join(tex_dir, files.get(base_name, base_name))
    size = dds_size(base_path)
    if size is None:
        return None
    bw, bh = size
    scale = min(1.0, float(cap) / max(bw, bh))
    w, h = max(4, int(bw * scale)), max(4, int(bh * scale))
    base = to_rgba(base_path, tmp, w, h)
    det_path = os.path.join(tex_dir, files.get(det_name, det_name))
    dsize = dds_size(det_path)
    if base is None or dsize is None:
        return None
    det = to_rgba(det_path, tmp, min(dsize[0], 512), min(dsize[1], 512))
    if det is None:
        return None

    props = mat.get("props", {})
    magic = float(props.get("magicMult", 1.0)) or 1.0
    dmean = det[:, :, :3].reshape(-1, 3).mean(axis=0)
    out = np.clip(base[:, :, :3] * (magic / second_stage_gain(dmean)), 0.0, 1.0)

    alpha = blend_alpha(mat, base, tex_dir, tmp, files, w, h)

    stem = mat["rf2_material"] + "_base"
    tga = os.path.join(tmp, stem + ".tga")
    write_tga(tga, out, alpha)
    run([texconv, "-f", "BC1_UNORM" if alpha is None else "BC3_UNORM",
         "-m", "0", SEPALPHA, "-y", "-o", tex_dir, tga])
    made = os.path.join(tex_dir, stem + ".DDS")
    final = os.path.join(tex_dir, stem + ".dds")
    if os.path.exists(made) and made != final:
        os.replace(made, final)
    return (stem + ".dds") if os.path.exists(final) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", help="out/<track> from convert_track.py")
    ap.add_argument("--grain", type=float, default=2.0,
                    help="how finely detail layers tile inside a baked texture")
    ap.add_argument("--density", type=float, default=TARGET_DENSITY,
                    help="texels per metre to aim for on ground surfaces")
    ap.add_argument("--size", type=int, default=12288,
                    help="largest dimension of a baked texture")
    ap.add_argument("--detail-metres", type=float, default=2.0,
                    help="how often a second stage detail repeats on the ground")
    ap.add_argument("--no-two-stage", action="store_true",
                    help="bake everything instead of using rF2's two texture shader")
    ap.add_argument("--texconv", default=TEXCONV)
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out_dir)
    tex_dir = os.path.join(out_dir, "textures")
    map_path = os.path.join(out_dir, "material_map.json")
    data = json.load(open(map_path))
    files = data.get("texture_files", {})

    tmp = os.path.join(out_dir, "_bake")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)

    # Clear what a previous run composed. A material that has since moved from
    # a bake to two stages would otherwise leave its old bake behind, and the
    # install would carry a texture nothing reads.
    #
    # Never by name alone: drift ships an AC texture called ext_base.dds, and
    # matching the suffix deleted it. Anything AC gave us is off limits.
    source = set(files.values()) | set(files)
    dropped = 0
    for f in os.listdir(tex_dir):
        if f.endswith(("_baked.dds", "_base.dds")) and f not in source:
            os.remove(os.path.join(tex_dir, f))
            dropped += 1
    if dropped:
        print("cleared %d composed textures from the last run" % dropped)

    seen, baked, repeats = {}, {}, {}
    for m in data["meshes"]:
        if wants_bake(m):
            seen.setdefault(m["rf2_material"], m)

    print("materials to compose: %d" % len(seen))
    two_stage = {}
    for name, m in sorted(seen.items()):
        m["_files"] = files

        # Hand to rF2 whatever rF2 can do itself.
        found = None if args.no_two_stage else single_detail(
            m, tex_dir, tmp, files, name=name, texconv=args.texconv)
        det, painted = found if found else (None, False)
        composed = None
        if det is not None:
            dmean = detail_mean(det, tex_dir, tmp, files)
            if painted:
                # The mask still decides where each layer's colour falls, and
                # only a bake can hold that. It carries the colours alone now,
                # with the grain left to the second stage, so the bake's own
                # resolution stops being the ceiling on how fine the surface
                # can look.
                got = bake_material(m, tex_dir, tmp, args.size, args.density,
                                    args.grain, lowfreq=True, second_mean=dmean)
                if got is not None:
                    composed = write_dds(name + "_base", got[0], got[2],
                                         tex_dir, tmp, args.texconv)
            else:
                composed = write_base(m, det, tex_dir, tmp, args.size, files,
                                      args.texconv)
        if composed is not None:
            # One square repeat on the ground, the same size both ways. A
            # road covers 171 metres in one UV unit along its length and 12
            # across it, so a single UV factor for both axes stretches the
            # grain by that ratio: AC's multiplier applied that way smeared
            # every road, sand and grass detail 3 to 26 times along the track.
            # Back to the signed-off 2 m for everything: AC's coarser repeat
            # (repeat_metres) looked worse in the sim, stretched or not.
            metres = args.detail_metres
            uv_b = []
            for span in (float(m.get("m_per_u", 0.0)),
                         float(m.get("m_per_v", 0.0))):
                f = span / metres if span > 0 else 1.0
                # The old bounds: a huge terrain sheet keeps the 512 it was
                # signed off with rather than tiling finer.
                uv_b.append(max(1.0, min(512.0, f)))
            ac_f = metres
            # The asphalt under the rubber at Highlands' density. Its road --
            # the only one without the near-car band, which there sits under
            # the wheels -- packs four copies of a 1024 detail into each 2 m
            # tile along the road: 0.5 m a repeat. The band ends where the
            # detail's mips take over, and that distance shrinks with texel
            # density, so the same density puts ours under the car too. The
            # normal map stays on the 2 m tiling it was signed off with.
            if m.get("under_rubber"):
                two_nm = list(uv_b)
                k = metres / ROAD_DETAIL_METRES
                uv_b = [f * k for f in uv_b]
                ac_f = ROAD_DETAIL_METRES
            else:
                two_nm = None
            # AC's own tiling on the asphalt (meshUV x multR, Silverstone's
            # multR 1 = one tile over 44 x 132 m) was tried 2026-10-12: huge
            # stretched stones with the sky glinting off them. Reverted; the
            # near band is handled by softening the detail instead (below).
            two_stage[name] = {"base": composed,
                               "detail": files.get(det, det),
                               "uv_b": uv_b}
            if m.get("_detail_nm"):
                two_stage[name]["detail_nm"] = m["_detail_nm"]
            if two_nm:
                two_stage[name]["uv_nm"] = two_nm
            # The road's specular map: AC masks specular by txDiffuse's alpha
            # when there is no txMaps (Silverstone asphalt: mean 0.41, 0.07 to
            # 0.87). Without one rF2 got the black stand-in and the road had
            # no sun glare at all, unlike the official tracks.
            if m.get("under_rubber") and not m["textures"].get("txMaps"):
                sm = road_spec_map(m, tex_dir, tmp, files, args.texconv)
                if sm:
                    two_stage[name]["spec"] = sm
                    print("  %-22s specular map from AC's diffuse alpha: %s"
                          % (name[:22], sm))
            print("  %-22s -> %-24s x %-20s %s %s (uv %.3g x %.3g)"
                  % (name[:22], composed, files.get(det, det)[:20],
                     "painted" if painted else "flat  ",
                     "every %.1fm" % ac_f,
                     uv_b[0], uv_b[1]))
            continue
        if det is not None:
            print("  %-22s two stage failed, baking instead" % name[:22])

        got = bake_material(m, tex_dir, tmp, args.size, args.density, args.grain)
        if got is None:
            print("  %-22s skipped (missing base, mask or details)" % name[:22])
            continue
        rgb, repeat, alpha = got
        final = write_dds(name + "_baked", rgb, alpha, tex_dir, tmp, args.texconv)
        if final is None:
            print("  %-22s texconv failed" % name[:22])
            continue
        baked[name] = final
        repeats[name] = repeat
        print("  %-22s -> %-24s %s" % (name[:22], final,
              ("uv x%.1f" % repeat) if repeat > 1.01 else ""))

    for m in data["meshes"]:
        if m["rf2_material"] in baked:
            m["baked"] = baked[m["rf2_material"]]
            m["uv_repeat"] = repeats.get(m["rf2_material"], 1.0)
        m.pop("two_stage", None)
        if m["rf2_material"] in two_stage:
            m["two_stage"] = two_stage[m["rf2_material"]]
    # The near-car band on the asphalt. Its detail's stored mips, decoded
    # (2026-10-12), lose contrast fast with distance: Silverstone's
    # tarmac-detail5 0.52 0.55 0.48 0.21 0.05 (std/mean, mip 0..4), so the
    # grain is strong for a few metres and then drops out at the mip 2 -> 3
    # step -- the edge the user sees. Scaling the whole texture down
    # (DETAIL_MAX_CONTRAST) kept every ratio between mips and so kept the
    # edge. The fix is a mip chain of even contrast: the same grain strength
    # out to mip 3, then a gentle fade, so nothing changes at any one distance.
    evened = {}
    for m in data["meshes"]:
        ts = two_stage.get(m["rf2_material"])
        if not SOFTEN_DETAIL:
            break
        if not ts or not m.get("under_rubber") or ts.get("_soft"):
            continue
        det = ts["detail"]
        if det not in evened:
            evened[det] = None
            path = os.path.join(tex_dir, det)
            sz = dds_size(path)
            img = to_rgba(path, tmp, sz[0], sz[1]) if sz else None
            if img is not None:
                alpha = img[:, :, 3] if float(img[:, :, 3].std()) > 0.01 else None
                if EVEN_MIPS:
                    evened[det] = even_mip_detail(
                        img[:, :, :3], alpha, os.path.splitext(det)[0] + "_even",
                        tex_dir, tmp, args.texconv)
                else:
                    # Softened about its mean to Highlands' 0.34, every mip
                    # alike: texconv builds the chain from the softened top.
                    rgb = img[:, :, :3]
                    lum = rgb.mean(axis=2)
                    rel = float(lum.std() / max(lum.mean(), 1e-3))
                    k = min(1.0, DETAIL_MAX_CONTRAST / max(rel, 1e-3))
                    mean = rgb.reshape(-1, 3).mean(axis=0)
                    out = write_dds(os.path.splitext(det)[0] + "_soft",
                                    mean + (rgb - mean) * k, alpha,
                                    tex_dir, tmp, args.texconv)
                    evened[det] = out and (out, "%.2f" % rel, "%.2f" % (rel * k))
        if evened[det]:
            out, before, after = evened[det]
            ts["detail"], ts["_soft"] = out, True
            print("  %-22s detail mips %s -> %s: %s"
                  % (m["rf2_material"][:22], before, after, out))
    for ts in two_stage.values():
        ts.pop("_soft", None)

    # The driving surface's detail one mip down. Near the car rF2 draws a
    # detail's full resolution -- 1024 texels across a 2 m tile, 2 mm each,
    # far finer than AC ever shows it (AC spreads the same texture 20 to 60
    # times wider) -- and switches to the next mip a couple of metres out.
    # The user saw the repeat only inside that band, ending at a sharp line,
    # with the road normal beyond it. Shipping the half size copy puts what
    # is drawn beyond the line in front of the car too.
    halved = {}
    for m in data["meshes"]:
        ts = two_stage.get(m["rf2_material"])
        # Off: the near band was the detail normal map tiled at 2 m, not the
        # detail's resolution (see blender_stage). Kept for reference.
        if not HALVE_ROAD_DETAIL:
            break
        if not ts or not m.get("track_surface") or ts.get("_half"):
            continue
        det = ts["detail"]
        if det not in halved:
            halved[det] = None
            path = os.path.join(tex_dir, det)
            sz = dds_size(path)
            if sz and min(sz) >= 256:
                img = to_rgba(path, tmp, sz[0] // 2, sz[1] // 2)
                if img is not None:
                    alpha = img[:, :, 3] if float(img[:, :, 3].std()) > 0.01 else None
                    halved[det] = write_dds(os.path.splitext(det)[0] + "_half",
                                            img[:, :, :3], alpha, tex_dir, tmp,
                                            args.texconv)
        if halved[det]:
            ts["detail"], ts["_half"] = halved[det], True
            print("  %-22s road detail one mip down: %s" % (m["rf2_material"][:22], halved[det]))
    for ts in two_stage.values():
        ts.pop("_half", None)
    data["baked"] = baked
    data["uv_repeat"] = repeats
    data["two_stage"] = two_stage

    # Decided from the texture that will actually ship rather than from AC's
    # ksAlphaRef, which is a figure from a different renderer: AC's 0.1 lets
    # nine tenths of the alpha range through, and CSP cuts foliage its own way
    # on top. A material whose texture turns out not to be a mask loses its
    # alpha test altogether instead of being given a fallback threshold.
    refs, ac_of, disk_of = {}, {}, {}
    for m in data["meshes"]:
        if not m.get("alpha_test"):
            continue
        name = m["rf2_material"]
        if name in refs:
            continue
        tex = m.get("baked")
        disk = tex if tex else files.get(m["textures"].get("txDiffuse"),
                                        m["textures"].get("txDiffuse"))
        if not disk:
            continue
        path = os.path.join(tex_dir, disk)
        ac = float(m.get("props", {}).get("ksAlphaRef", 0.0)) or 0.5
        ac = max(1, min(254, int(round(ac * 255.0))))
        refs[name] = cutout_ref(path, tmp, ac) if os.path.exists(path) else GRADIENT
        # A soft edge drawn whole is right for a ground blend like Laguna's
        # gravel border, which lies on the surface it fades into. Standing
        # up it is a billboard: AC still cuts it at its threshold, and drawn
        # whole the card shows as a square. Spa's bbgrass (ksPerPixelAT,
        # ref 0.5) came out as dark squares across the infield that way.
        if refs[name] == GRADIENT and os.path.exists(path) \
                and float(m.get("upward", 0.0)) < 0.5:
            refs[name] = CUTOUT_REF
        ac_of[name], disk_of[name] = ac, disk
    # Only hard masks get a threshold, and only a threshold gets colour keyed.
    # The racing line rubber stays exactly as AC ships it. A copy at twice
    # the opacity, in grey, raised the contrast as measured but read as
    # artificially dark, and the user asked for the rubber as it was with only
    # the road brightened -- which ROAD_LIFT below does on its own.
    data["rubber_tex"] = {}

    # The road under it, lifted back to where it sat on screen before.
    for m in data["meshes"]:
        nm = m["rf2_material"]
        ts = two_stage.get(nm)
        if not ts or not m.get("under_rubber") or ts.get("_lifted"):
            continue
        path = os.path.join(tex_dir, ts["base"])
        sz = dds_size(path)
        img = to_rgba(path, tmp, sz[0], sz[1]) if sz else None
        if img is None:
            continue
        alpha = img[:, :, 3] if float(img[:, :, 3].std()) > 0.01 else None
        out = write_dds(os.path.splitext(ts["base"])[0],
                        np.clip(img[:, :, :3] * ROAD_LIFT, 0.0, 1.0), alpha,
                        tex_dir, tmp, args.texconv)
        if out:
            ts["_lifted"] = True
            # Recorded so the exporter knows this much of the brightness is
            # already in the texture and does not apply it again.
            ts["lift"] = ROAD_LIFT
            print("  %-22s road under the rubber lifted x%.3f" % (nm[:22], ROAD_LIFT))
    for ts in two_stage.values():
        ts.pop("_lifted", None)

    # The terrain, held to AC's balance against the road.
    #
    # In AC the terrain is lit far harder than the asphalt: CSP gives it
    # 0.72 / 0.40 against the road's 0.42 / 0.35 (the kn5 alone says 0.60 /
    # 0.70 against 0.38 / 0.42). Under our light that is 1.41x the road for
    # the same texture. rF2's native 1.0 put the two at parity, and the old
    # scheme clamped the terrain at 1.0 for the same result, so the hills have
    # been about 40% too dark in every build -- the "mud", with the rocks far
    # duller than in AC. Diffuse cannot go above 1.0, so the lift goes into the
    # terrain's own base, on top of the road's lift so the two keep their
    # ratio. Off-track ground with CSP's figures only; the racing surface is
    # signed off as it is.
    # Against the anchor (ANCHOR_KD / ANCHOR_KA), not this track's road: on
    # Laguna the two are the same thing, so its terrain is unchanged.
    r_kd, r_ka = ANCHOR_KD, ANCHOR_KA
    done = set()
    for m in data["meshes"]:
        nm = m["rf2_material"]; ts = two_stage.get(nm); pr = m.get("props", {})
        if nm in done or not ts or m.get("track_surface") or m.get("under_rubber"):
            continue
        if float(m.get("flatness", 0.0)) <= 0.5:
            continue
        # CSP's figures where it has them, the kn5's where it does not.
        kd = float(pr.get("csp_ksDiffuse", pr.get("ksDiffuse", 0.0)) or 0.0)
        ka = float(pr.get("csp_ksAmbient", pr.get("ksAmbient", kd)) or 0.0)
        if kd <= 0 and ka <= 0:
            continue
        combo = (SCENE_AMBIENT * ka / r_ka + SCENE_SUN_FLAT * kd / r_kd) /                 (SCENE_AMBIENT + SCENE_SUN_FLAT)
        lift = combo * ROAD_LIFT
        if lift <= 1.0:
            continue
        path = os.path.join(tex_dir, ts["base"])
        sz = dds_size(path)
        img = to_rgba(path, tmp, sz[0], sz[1]) if sz else None
        if img is None:
            continue
        alpha = img[:, :, 3] if float(img[:, :, 3].std()) > 0.01 else None
        if write_dds(os.path.splitext(ts["base"])[0],
                     np.clip(img[:, :, :3] * lift, 0.0, 1.0), alpha,
                     tex_dir, tmp, args.texconv):
            done.add(nm)
            ts["lift"] = round(float(lift), 4)
            print("  %-22s terrain lifted x%.3f to AC's balance against the road"
                  % (nm[:22], lift))

    # What each material's diffuse actually averages, measured on the texture
    # that ships. The exporter needs it to aim the reflectance at rF2's own
    # albedo band instead of writing a flat 1.0 and letting a bright AC
    # texture come out at an albedo rF2 never authors.
    lums, p90, rgbs = {}, {}, {}
    for m in data["meshes"]:
        nm = m["rf2_material"]
        if nm in lums:
            continue
        tex = m.get("baked")
        if not tex:
            ts = m.get("two_stage") or {}
            tex = ts.get("base")
        disk = tex if tex else files.get(m["textures"].get("txDiffuse"),
                                        m["textures"].get("txDiffuse"))
        if not disk:
            continue
        path = os.path.join(tex_dir, disk)
        if not os.path.exists(path):
            continue
        sz = dds_size(path)
        if not sz:
            continue
        img = to_rgba(path, tmp, min(sz[0], 512), min(sz[1], 512))
        if img is None:
            continue
        alpha = img[:, :, 3]
        rgb = img[:, :, :3]
        # Its mean colour, for telling green ground from sand among the
        # materials CSP grows GrassFX on.
        sel = alpha >= 0.75 if (alpha < 0.75).mean() > 0.05 else np.ones(alpha.shape, bool)
        if sel.any():
            col = rgb[sel].mean(axis=0)
            # A two stage base is pre-divided by its detail and comes out near
            # white; the grass green is in the detail. Their product is what
            # the surface shows.
            det = (m.get("two_stage") or {}).get("detail")
            dpath = os.path.join(tex_dir, det) if det else None
            if dpath and not tex == m.get("baked") and os.path.exists(dpath):
                dsz = dds_size(dpath)
                dimg = to_rgba(dpath, tmp, min(dsz[0], 256), min(dsz[1], 256)) if dsz else None
                if dimg is not None:
                    col = col * dimg[:, :, :3].reshape(-1, 3).mean(axis=0)
            rgbs[nm] = [round(float(c) * 255.0, 1) for c in col]
        # Where the material's own triangles point on the texture, if known.
        # A wall that uses one bright corner of a big atlas averages dark
        # over the whole atlas and was never corrected; this measures the
        # part that is actually drawn.
        uvs = (data.get("uv_samples") or {}).get(m["ac_material"])
        if uvs:
            hh, ww = rgb.shape[:2]
            uv = np.asarray(uvs, dtype=float)
            rr = np.clip((uv[:, 1] * hh).astype(int), 0, hh - 1)
            cc = np.clip((uv[:, 0] * ww).astype(int), 0, ww - 1)
            s_rgb, s_a = rgb[rr, cc], alpha[rr, cc]
            keep = s_a >= 0.75 if (s_a < 0.75).mean() > 0.05 else np.ones(len(s_a), bool)
            if keep.sum() >= 20:
                drawn = s_rgb[keep].mean(axis=1)
                lums[nm] = round(float(drawn.mean()) * 255.0, 1)
                # The bright tenth of what is drawn. An average hides a white
                # wall among dark doors: Pitbox averages 105.6 but its
                # brightest tenth is 156.7, which is the part seen as too
                # bright.
                p90[nm] = round(float(np.percentile(drawn, 90)) * 255.0, 1)
                continue
        pick = alpha >= 0.75 if (alpha < 0.75).mean() > 0.05 else None
        vals = rgb[pick] if pick is not None and pick.any() else rgb.reshape(-1, 3)
        lums[nm] = round(float(vals.mean()) * 255.0, 1)
    data["albedo_lum"] = lums
    data["albedo_p90"] = p90
    data["albedo_rgb"] = rgbs
    # AC masks specular with the diffuse texture's alpha where a material has
    # no txMaps; rF2 gets a flat specular map instead, so the exporter scales
    # the figure by this. Silverstone's grass: ksSpecular 1.0, alpha 0.23.
    spec_mask = {}
    for m in data["meshes"]:
        nm = m["rf2_material"]
        if nm in spec_mask or not m.get("render", True):
            continue
        tx = m["textures"].get("txDiffuse")
        src = files.get(tx, tx) if tx else None
        path = os.path.join(tex_dir, src) if src else None
        if not path or not os.path.exists(path):
            continue
        sz = dds_size(path)
        img = to_rgba(path, tmp, min(sz[0], 256), min(sz[1], 256)) if sz else None
        if img is not None:
            spec_mask[nm] = round(float(img[:, :, 3].mean()), 3)
    data["spec_mask"] = spec_mask

    # Every hard cut-out at the threshold AC authored it with, on a copy of its
    # texture whose mips are rebuilt to hold that threshold's coverage.
    #
    # 191 was a workaround. A low threshold filled cut-outs in at a distance
    # because building a mip averages the transparent background into the
    # leaves, so at 26 the grass atlas went from 33% passing at full size to
    # 77% at 16x16. Forcing 191 stopped that, but it also stripped every thin
    # grass blade back to the dense strip at its base -- the "rectangle of
    # grass" -- because blades are a texel or two wide and antialiased far
    # below 191. AC keeps them with 26 for exactly that reason, and uses 178
    # on the bushes in the very same texture.
    #
    # texconv's -keepcoverage rebuilds each mip so the share of texels passing
    # the threshold matches full size. That removes the fill-in by
    # construction, so the authored thresholds can be used as authored. One
    # copy per texture and threshold, because coverage is preserved for one
    # threshold at a time and BBGRASS_D serves both 26 and 178.
    hard = sorted(k for k, v in refs.items() if v == CUTOUT_REF)
    data["alpha_ref"], data["cutout_tex"] = {}, {}
    made = {}
    drawn = {}
    for m in data["meshes"]:
        if m.get("render", True):
            drawn.setdefault(m["rf2_material"], m)
    dim = albedo_caps(drawn, lums, p90, set(hard))
    data["dim_scale"] = dim
    for k in hard:
        scale = GRASS_CARD_TEXTURE if is_grass_card(drawn.get(k)) else dim.get(k, 1.0)
        key = (disk_of[k], ac_of[k], scale)
        if key not in made:
            made[key] = coverage_copy(disk_of[k], ac_of[k], tex_dir, tmp,
                                      args.texconv, rgb_scale=scale)
            if scale != 1.0:
                print("   %-18s %s texture x%.2f" % (
                    k[:18], "grass card" if k not in dim else "bright cut-out", scale))
        data["alpha_ref"][k] = CUTOUT_REF
        if made[key]:
            data["cutout_tex"][k] = made[key]
    # The rest of the over-bright materials: a darkened copy of their diffuse.
    data["dim_tex"] = {}
    dimmed = {}
    for k, scale in sorted(dim.items()):
        if k in hard:
            continue
        m = drawn[k]
        tx = m["textures"].get("txDiffuse")
        disk = files.get(tx, tx) if tx else None
        if not disk or not os.path.exists(os.path.join(tex_dir, disk)):
            continue
        key = (disk, scale)
        if key not in dimmed:
            dimmed[key] = dim_copy(disk, scale, tex_dir, tmp, args.texconv)
        if dimmed[key]:
            data["dim_tex"][k] = dimmed[key]
    if dim:
        print("bright textures darkened toward rF2's albedo band: %d materials, "
              "%d copies" % (len(dim), len([v for v in dimmed.values() if v])))
        for k, scale in sorted(dim.items(), key=lambda kv: kv[1])[:12]:
            print("   %-28s x%.2f  (albedo %s)" % (k[:28], scale, lums.get(k)))
    data["no_alpha_test"] = sorted(k for k, v in refs.items() if v is None)
    data["gradients"] = sorted(k for k, v in refs.items() if v == GRADIENT)
    print("alpha tests: %d hard cut-outs at %d, alpha remapped from AC's own "
          "threshold, mips holding coverage; %d gradients drawn whole, %d not "
          "masks and drawn whole" % (len(data["alpha_ref"]), CUTOUT_REF,
                                     len(data["gradients"]), len(data["no_alpha_test"])))
    for k in hard:
        print("   %-18s AC %3d -> 191  %s" % (k[:18], ac_of[k],
                                             data["cutout_tex"].get(k, "(no copy)")))
    for k in data["gradients"]:
        print("   gradient, no test:    %s" % k)
    for k in data["no_alpha_test"]:
        print("   no alpha test:        %s" % k)
    json.dump(data, open(map_path, "w"), indent=1)
    shutil.rmtree(tmp, ignore_errors=True)
    print("baked %d materials, %d left to rF2 as two stage"
          % (len(baked), len(two_stage)))


if __name__ == "__main__":
    main()
