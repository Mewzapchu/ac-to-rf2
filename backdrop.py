"""Cut fully transparent regions out of a mesh so it can be drawn opaque.

A painted horizon backdrop is mostly solid mountain with clear sky above it.
Drawn opaque it sorts correctly but the sky is a lit band; drawn with alpha
blending the sky is gone but it no longer writes depth, so it paints over the
trees in front of it.

Alpha testing would give both, but the exporter writes a fixed shader name and
the material flag that would enable it is undocumented. Removing the
transparent triangles from the geometry achieves the same result: what is left
is genuinely opaque, writes depth, and sorts against the scene normally.
"""
import struct

TRANSPARENT = 24        # 0-255; below this a texel counts as clear
MAX_DEPTH = 5           # subdivision levels, so the silhouette follows the ridge


def alpha_grid(path):
    """Coarse alpha coverage of a DXT5 texture, one value per 4x4 block."""
    with open(path, "rb") as f:
        d = f.read()
    if len(d) < 132 or d[:4] != b"DDS " or d[84:88] != b"DXT5":
        return None
    h, w = struct.unpack_from("<II", d, 12)
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    grid = bytearray(bw * bh)
    for i in range(bw * bh):
        off = 128 + i * 16
        if off + 2 > len(d):
            break
        # the two endpoints bound every alpha value the block can produce
        grid[i] = (d[off] + d[off + 1]) // 2
    return grid, bw, bh


def sample(grid, bw, bh, u, v):
    x = int((u % 1.0) * bw)
    y = int(((1.0 - v) % 1.0) * bh)
    x = min(max(x, 0), bw - 1)
    y = min(max(y, 0), bh - 1)
    return grid[y * bw + x]


def clip(verts, uvs, faces, tex):
    """Drop triangles whose texture is clear, subdividing along the boundary."""
    got = alpha_grid(tex)
    if got is None:
        return verts, uvs, faces, 0
    grid, bw, bh = got

    out_v = list(verts)
    out_uv = list(uvs)
    out_f = []
    dropped = 0

    def add(p, t):
        out_v.append(p)
        out_uv.append(t)
        return len(out_v) - 1

    def mid(a, b):
        pa, pb = out_v[a], out_v[b]
        ta, tb = out_uv[a], out_uv[b]
        return add(tuple((pa[i] + pb[i]) / 2.0 for i in range(3)),
                   ((ta[0] + tb[0]) / 2.0, (ta[1] + tb[1]) / 2.0))

    def alpha_at(i):
        return sample(grid, bw, bh, out_uv[i][0], out_uv[i][1])

    def recurse(a, b, c, depth):
        nonlocal dropped
        va, vb, vc = alpha_at(a), alpha_at(b), alpha_at(c)
        lo = min(va, vb, vc)
        hi = max(va, vb, vc)
        if hi < TRANSPARENT:            # wholly clear, discard
            dropped += 1
            return
        if lo >= TRANSPARENT or depth >= MAX_DEPTH:
            if hi >= TRANSPARENT:
                out_f.append((a, b, c))
            else:
                dropped += 1
            return
        ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
        recurse(a, ab, ca, depth + 1)
        recurse(ab, b, bc, depth + 1)
        recurse(ca, bc, c, depth + 1)
        recurse(ab, bc, ca, depth + 1)

    for f in faces:
        recurse(f[0], f[1], f[2], 0)

    # drop vertices nothing references any more
    used = sorted({i for f in out_f for i in f})
    remap = {old: new for new, old in enumerate(used)}
    return ([out_v[i] for i in used],
            [out_uv[i] for i in used],
            [tuple(remap[i] for i in f) for f in out_f],
            dropped)
