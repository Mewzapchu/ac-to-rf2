"""Generate an rFactor 2 .AIW from an Assetto Corsa track.

Grid and pit spots come from the AC_START and AC_PIT dummies in the kn5. The
driving path is derived from the tarmac itself: AC only ships a racing line for
circuits, and tracks like drift pads have an empty one, so a loop is fitted
inside the road surface instead.

AC world coordinates pass through the kn5 -> Blender -> gmt chain unchanged,
verified by matching float values in an exported gmt, so no axis conversion is
applied here.
"""
import os
import math
import struct
import json
import argparse

import numpy as np

from kn5 import KN5

BINS = 144
INSET = 0.55          # fraction of the road radius the path sits at
SPACING = 6.0         # metres between waypoints
SECTORS = 3
GARAGE_BACK = 6.0   # metres behind the pit box


def mirror_pos(p):
    return (-p[0], p[1], p[2])


def mirror_yaw(y):
    return -y


def face_out(yaw):
    """Turn an AC pit marker to face the way rF2 wants a car to sit.

    AC's AC_PIT markers point into the box, the way the car arrives. rF2 reads
    the orientation as the way the car faces once parked, which is the other
    way round, so a car spawned straight from the marker sits backwards in its
    own pit.
    """
    return (yaw + math.pi + math.pi) % (2.0 * math.pi) - math.pi


def mul(a, b):
    r = [0.0] * 16
    for i in range(4):
        for j in range(4):
            r[i * 4 + j] = sum(a[i * 4 + t] * b[t * 4 + j] for t in range(4))
    return r


def world_matrix(node):
    chain = []
    n = node
    while n is not None:
        if n.matrix:
            chain.append(n.matrix)
        n = n.parent
    m = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    for c in chain:
        m = mul(m, c)
    return m


def transform(m, p):
    x, y, z = p
    return (x * m[0] + y * m[4] + z * m[8] + m[12],
            x * m[1] + y * m[5] + z * m[9] + m[13],
            x * m[2] + y * m[6] + z * m[10] + m[14])


def yaw_of(m):
    """Forward is the third row of a row major matrix."""
    return math.atan2(m[8], m[10])



def read_fast_lane(track_dir):
    """AC's own racing line, if the track ships one.

    Layout is a four int header then one record per point: position, distance
    along the lap, and an index. Tracks with no AI, such as drift pads, ship a
    stub of a few bytes and fall back to a generated loop.
    """
    path = os.path.join(track_dir, "ai", "fast_lane.ai")
    if not os.path.exists(path) or os.path.getsize(path) < 64:
        return []
    with open(path, "rb") as f:
        d = f.read()
    ver, count, _lap, _samples = struct.unpack_from("<4i", d, 0)
    if ver not in (7,) or count <= 0 or 16 + count * 20 > len(d):
        return []
    pts = []
    for i in range(count):
        x, y, z = struct.unpack_from("<3f", d, 16 + i * 20)
        pts.append((x, y, z))
    return pts


def resample_line(pts, spacing):
    """Even spacing around a closed line."""
    n = len(pts)
    seg, total = [], 0.0
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        d = math.hypot(b[0] - a[0], b[2] - a[2])
        seg.append(d)
        total += d
    count = max(16, int(total / spacing))
    step = total / count
    out, idx, acc = [], 0, 0.0
    for j in range(count):
        target = j * step
        while idx < n - 1 and acc + seg[idx] < target:
            acc += seg[idx]
            idx += 1
        a, b = pts[idx], pts[(idx + 1) % n]
        t = (target - acc) / seg[idx] if seg[idx] > 1e-6 else 0.0
        out.append(tuple(a[k] + (b[k] - a[k]) * t for k in range(3)))
    return out, total


def collect(k, valid_keys=None):
    """Split the model into spawn markers, collidable surfaces and drivable ones.

    Which surfaces count as drivable comes from AC's own surfaces file, where
    each one is flagged valid or not. Matching on the name "ROAD" only works on
    tracks that happen to use it; real circuits use names like ASPHNEW or RDOLD.
    """
    starts, pits, road, solid = [], [], [], []
    for n in k.walk():
        if n.type == 1 and n.matrix:
            if n.name.startswith("AC_START"):
                m = world_matrix(n)
                starts.append(((m[12], m[13], m[14]), yaw_of(m), n.name))
            elif n.name.startswith("AC_PIT"):
                m = world_matrix(n)
                pits.append(((m[12], m[13], m[14]), yaw_of(m), n.name))
        elif n.type in (2, 3) and n.verts:
            up = n.name.upper()
            if not up[:1].isdigit():
                continue
            stripped = up.lstrip("0123456789")
            drivable = False
            if valid_keys:
                drivable = any(stripped.startswith(k) for k in valid_keys)
            else:
                drivable = "ROAD" in up
            m = world_matrix(n)
            pts = [transform(m, v) for v in n.verts]
            idx = n.indices
            for i in range(0, n.index_count, 3):
                a, b, c = pts[idx[i]], pts[idx[i + 1]], pts[idx[i + 2]]
                tri = (((a[0], a[2]), (b[0], b[2]), (c[0], c[2])),
                       (a[1], b[1], c[1]))
                solid.append(tri)
                if drivable:
                    road.append(tri)

    def order(item):
        tail = item[2].rsplit("_", 1)[-1]
        return int(tail) if tail.isdigit() else 0

    starts.sort(key=order)
    pits.sort(key=order)
    return starts, pits, road, solid


class SurfaceProbe:
    """Exact surface heights straight below a point, every layer of them.

    RoadGrid keeps one averaged height per cell, which is right on a single
    layer of ground but not where surfaces stack: on ek_nagao the road at the
    start sits 20 m above a lower stretch of the mountain, the average landed
    halfway between, and the grid spawned the cars underground.
    """

    def __init__(self, tris):
        a = np.array([[t[0][0], t[0][1], t[1][0], t[1][1], t[2][0], t[2][1],
                       ys[0], ys[1], ys[2]] for t, ys in tris], np.float64)
        self.x1, self.z1, self.x2, self.z2, self.x3, self.z3 = a[:, :6].T
        self.y = a[:, 6:9]
        self.den = ((self.z2 - self.z3) * (self.x1 - self.x3)
                    + (self.x3 - self.x2) * (self.z1 - self.z3))
        self.minx = np.minimum(np.minimum(self.x1, self.x2), self.x3)
        self.maxx = np.maximum(np.maximum(self.x1, self.x2), self.x3)
        self.minz = np.minimum(np.minimum(self.z1, self.z2), self.z3)
        self.maxz = np.maximum(np.maximum(self.z1, self.z2), self.z3)

    def heights(self, x, z):
        k = np.nonzero((self.minx <= x) & (x <= self.maxx) & (self.minz <= z)
                       & (z <= self.maxz) & (np.abs(self.den) > 1e-9))[0]
        if not len(k):
            return []
        d = self.den[k]
        a = ((self.z2[k] - self.z3[k]) * (x - self.x3[k])
             + (self.x3[k] - self.x2[k]) * (z - self.z3[k])) / d
        b = ((self.z3[k] - self.z1[k]) * (x - self.x3[k])
             + (self.x1[k] - self.x3[k]) * (z - self.z3[k])) / d
        inside = (a >= -1e-6) & (b >= -1e-6) & (a + b <= 1 + 1e-6)
        ys = self.y[k]
        h = a * ys[:, 0] + b * ys[:, 1] + (1 - a - b) * ys[:, 2]
        return sorted(float(v) for v in h[inside])

    def below(self, x, y, z, reach=3.0):
        """The highest surface under (x, y, z), or None. AC's markers float a
        metre or so up; `reach` allows for one sitting slightly in the road."""
        hs = [h for h in self.heights(x, z) if h <= y + reach]
        return hs[-1] if hs else None


class RoadGrid:
    """One metre occupancy grid of the drivable surface, plus clearance.

    A radial fit around the road centroid puts more than half the path on grass
    for anything that is not roughly circular, and rF2 then reports missing
    sector polygons. Working from actual coverage avoids guessing at the shape.
    """

    def __init__(self, tris, cell=1.0):
        self.cell = cell
        xs = [p[0] for t, _ys in tris for p in t]
        zs = [p[1] for t, _ys in tris for p in t]
        self.minx, self.maxx = min(xs), max(xs)
        self.minz, self.maxz = min(zs), max(zs)
        self.w = int((self.maxx - self.minx) / cell) + 2
        self.h = int((self.maxz - self.minz) / cell) + 2
        self.grid = bytearray(self.w * self.h)
        self.ysum = [0.0] * (self.w * self.h)
        self.ycnt = [0] * (self.w * self.h)
        for t, ys in tris:
            self._raster(t, ys)

    def _raster(self, t, ys):
        xs = [p[0] for p in t]
        zs = [p[1] for p in t]
        (x1, z1), (x2, z2), (x3, z3) = t
        den = (z2 - z3) * (x1 - x3) + (x3 - x2) * (z1 - z3)
        if abs(den) < 1e-9:
            return
        for gx in range(self.gx(min(xs)), self.gx(max(xs)) + 1):
            for gz in range(self.gz(min(zs)), self.gz(max(zs)) + 1):
                if not (0 <= gx < self.w and 0 <= gz < self.h):
                    continue
                px = self.minx + (gx + 0.5) * self.cell
                pz = self.minz + (gz + 0.5) * self.cell
                a = ((z2 - z3) * (px - x3) + (x3 - x2) * (pz - z3)) / den
                b = ((z3 - z1) * (px - x3) + (x1 - x3) * (pz - z3)) / den
                if a >= 0 and b >= 0 and a + b <= 1:
                    i = gz * self.w + gx
                    self.grid[i] = 1
                    self.ysum[i] += sum(ys) / 3.0
                    self.ycnt[i] += 1

    def gx(self, x):
        return int((x - self.minx) / self.cell)

    def gz(self, z):
        return int((z - self.minz) / self.cell)

    def on_road(self, x, z):
        gx, gz = self.gx(x), self.gz(z)
        return 0 <= gx < self.w and 0 <= gz < self.h and self.grid[gz * self.w + gx]

    def height(self, x, z, ref=None):
        # Near a known height (a marker, a waypoint), the first surface below
        # it: the cell average lies between layers where ground stacks.
        probe = getattr(self, "probe", None)
        if ref is not None and probe is not None:
            h = probe.below(x, ref, z)
            if h is not None:
                return h
        gx, gz = self.gx(x), self.gz(z)
        i = gz * self.w + gx
        if 0 <= gx < self.w and 0 <= gz < self.h and self.ycnt[i]:
            return self.ysum[i] / self.ycnt[i]
        return 0.0

    def clearance(self):
        """Chamfer distance transform: metres from each cell to the nearest edge."""
        big = float(self.w + self.h)
        d = [0.0 if not self.grid[i] else big for i in range(self.w * self.h)]
        w, h = self.w, self.h
        for gz in range(h):
            for gx in range(w):
                i = gz * w + gx
                if d[i] == 0.0:
                    continue
                best = d[i]
                if gx > 0:
                    best = min(best, d[i - 1] + 1)
                if gz > 0:
                    best = min(best, d[i - w] + 1)
                if gx > 0 and gz > 0:
                    best = min(best, d[i - w - 1] + 1.414)
                if gx < w - 1 and gz > 0:
                    best = min(best, d[i - w + 1] + 1.414)
                d[i] = best
        for gz in range(h - 1, -1, -1):
            for gx in range(w - 1, -1, -1):
                i = gz * w + gx
                best = d[i]
                if gx < w - 1:
                    best = min(best, d[i + 1] + 1)
                if gz < h - 1:
                    best = min(best, d[i + w] + 1)
                if gx < w - 1 and gz < h - 1:
                    best = min(best, d[i + w + 1] + 1.414)
                if gx > 0 and gz < h - 1:
                    best = min(best, d[i + w - 1] + 1.414)
                d[i] = best
        return d


NEIGHBOURS = ((1, 0), (-1, 0), (0, 1), (0, -1),
              (1, 1), (1, -1), (-1, 1), (-1, -1))


def fit_loop(tris, seed=None):
    """Find the largest closed loop through the road network.

    The road is skeletonised to cells that sit well clear of any edge, which
    approximates a centreline. A breadth first tree over that skeleton turns
    every remaining edge into a candidate cycle, and the longest one is the
    biggest lap the layout offers.
    """
    g = RoadGrid(tris)
    dist = g.clearance()
    g.dist = dist
    w, h = g.w, g.h

    for thresh in (2.5, 2.0, 1.5, 1.0):
        wide = {i for i in range(w * h) if dist[i] >= thresh}
        if len(wide) < 200:
            continue
        skel = thin(wide, w, h)
        start = pick_start(g, skel, seed)
        cycle = largest_cycle(skel, start, w, h)
        if cycle and len(cycle) >= 60:
            pts = smooth(g, cycle)
            print("corridors at %.1f m, %d cells thinned to %d, loop of %d cells"
                  % (thresh, len(wide), len(skel), len(cycle)))
            return pts, g
    raise SystemExit("could not find a closed loop in the road surface")


# Zhang-Suen neighbours, clockwise from north
P_ORDER = ((0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1))


def thin(cells, w, h):
    """Zhang-Suen thinning down to a one cell wide centreline.

    Without this the wide parts of the circuit stay a blob, and a breadth first
    tree through a blob finds only short cycles rather than the actual lap.
    """
    fg = set(cells)
    while True:
        removed_any = False
        for step in (0, 1):
            remove = []
            for i in fg:
                x, z = i % w, i // w
                if x <= 0 or z <= 0 or x >= w - 1 or z >= h - 1:
                    continue
                p = [1 if (z + dz) * w + (x + dx) in fg else 0
                     for dx, dz in P_ORDER]
                b = sum(p)
                if b < 2 or b > 6:
                    continue
                a = sum(1 for k in range(8)
                        if p[k] == 0 and p[(k + 1) % 8] == 1)
                if a != 1:
                    continue
                if step == 0:
                    if p[0] * p[2] * p[4] or p[2] * p[4] * p[6]:
                        continue
                else:
                    if p[0] * p[2] * p[6] or p[0] * p[4] * p[6]:
                        continue
                remove.append(i)
            if remove:
                fg.difference_update(remove)
                removed_any = True
        if not removed_any:
            return fg


def pick_start(g, skel, seed):
    if seed is None:
        return max(skel, key=lambda i: g.dist[i])
    sx, sz = g.gx(seed[0]), g.gz(seed[2])
    return min(skel, key=lambda i: (i % g.w - sx) ** 2 + (i // g.w - sz) ** 2)


def largest_cycle(skel, start, w, h):
    parent = {start: None}
    depth = {start: 0}
    order = [start]
    qi = 0
    while qi < len(order):
        i = order[qi]
        qi += 1
        x, z = i % w, i // w
        for dx, dz in NEIGHBOURS:
            nx, nz = x + dx, z + dz
            if not (0 <= nx < w and 0 <= nz < h):
                continue
            j = nz * w + nx
            if j in skel and j not in depth:
                depth[j] = depth[i] + 1
                parent[j] = i
                order.append(j)

    candidates = []
    for i in depth:
        x, z = i % w, i // w
        for dx, dz in NEIGHBOURS:
            nx, nz = x + dx, z + dz
            if not (0 <= nx < w and 0 <= nz < h):
                continue
            j = nz * w + nx
            if j not in depth or j <= i or j == parent.get(i) or i == parent.get(j):
                continue
            candidates.append((depth[i] + depth[j], i, j))
    if not candidates:
        return None
    candidates.sort(reverse=True)

    def cycle_len(i, j):
        """Exact loop length: the shared prefix back to the meeting point cancels."""
        a, b, n = i, j, 0
        while depth[a] > depth[b]:
            a = parent[a]
            n += 1
        while depth[b] > depth[a]:
            b = parent[b]
            n += 1
        while a != b:
            a, b = parent[a], parent[b]
            n += 2
        return n

    best = None
    best_len = 0
    for total, i, j in candidates:
        # depth sum bounds the true length, so once it drops below the best
        # confirmed loop nothing later can beat it
        if total <= best_len:
            break
        n = cycle_len(i, j)
        if n > best_len:
            best_len, best = n, (i, j)
    if best is None:
        return None

    u, v = best
    au, av = [], []
    while depth[u] > depth[v]:
        au.append(u)
        u = parent[u]
    while depth[v] > depth[u]:
        av.append(v)
        v = parent[v]
    while u != v:
        au.append(u)
        av.append(v)
        u, v = parent[u], parent[v]
    au.append(u)
    return au + av[::-1]


def smooth(g, cycle, rounds=12):
    """Relax the cell path into a drivable line, never leaving the tarmac."""
    pts = [(g.minx + (i % g.w + 0.5) * g.cell,
            g.minz + (i // g.w + 0.5) * g.cell) for i in cycle]
    n = len(pts)
    for _ in range(rounds):
        new = []
        for i in range(n):
            ax, az = pts[(i - 1) % n]
            bx, bz = pts[i]
            cx, cz = pts[(i + 1) % n]
            sx, sz = (ax + 2 * bx + cx) / 4.0, (az + 2 * bz + cz) / 4.0
            new.append((sx, sz) if g.on_road(sx, sz) else (bx, bz))
        pts = new
    out = []
    for x, z in pts:
        gi = g.gz(z) * g.w + g.gx(x)
        out.append((x, g.height(x, z), z, g.dist[gi] * g.cell))
    return out


def resample(pts):
    """Even spacing around the closed loop."""
    n = len(pts)
    seg = []
    total = 0.0
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        d = math.hypot(b[0] - a[0], b[2] - a[2])
        seg.append(d)
        total += d
    count = max(16, int(total / SPACING))
    step = total / count
    out = []
    idx, acc = 0, 0.0
    for j in range(count):
        target = j * step
        while acc + seg[idx] < target and idx < n - 1:
            acc += seg[idx]
            idx += 1
        a, b = pts[idx], pts[(idx + 1) % n]
        t = (target - acc) / seg[idx] if seg[idx] > 1e-6 else 0.0
        out.append((a[0] + (b[0] - a[0]) * t,
                    a[1] + (b[1] - a[1]) * t,
                    a[2] + (b[2] - a[2]) * t,
                    a[3] + (b[3] - a[3]) * t))
    return out, total


def write_sector_polys(wps, path):
    """Timing triggers.

    rF2 looks for meshes literally named xfinish, xsector1 and xsector2, and
    refuses to build collision without them ("No sector 1 poly found"). Each is
    a flat strip laid across the path, slightly proud of the surface.
    """
    n = len(wps)
    out = []
    for name, idx in (("xfinish", 0),
                      ("xsector1", n // 3),
                      ("xsector2", 2 * n // 3)):
        x, y, z, half = wps[idx]
        nx, _, nz, _ = wps[(idx + 1) % n]
        fx, fz = nx - x, nz - z
        flen = math.hypot(fx, fz) or 1.0
        fx, fz = fx / flen, fz / flen
        px, pz = -fz, fx
        wide = max(6.0, half + 4.0)
        long = 1.0
        y += 0.03
        corners = [
            (x - px * wide - fx * long, y, z - pz * wide - fz * long),
            (x + px * wide - fx * long, y, z + pz * wide - fz * long),
            (x + px * wide + fx * long, y, z + pz * wide + fz * long),
            (x - px * wide + fx * long, y, z - pz * wide + fz * long),
        ]
        out.append({"name": name, "verts": corners,
                    "faces": [[0, 1, 2], [0, 2, 3]]})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print("wrote %d sector polys -> %s" % (len(out), path))



PIT_LANE_OFFSET = 11.0   # metres from the racing line to the pit lane
PIT_BOX_OFFSET = 5.0     # from the pit lane to the boxes
GARAGE_OFFSET = 5.0      # from the boxes to the garages



PIT_LANE_AHEAD = 7.0     # metres in front of a pit box, where the lane runs


def pitlane_from_ac(pits, wps, ground):
    """Build the pit lane from AC's own pit boxes.

    AC marks every real pit stall with an AC_PIT dummy facing the lane. Using
    those puts cars in the actual pit boxes, instead of the invented row beside
    the racing line that a generated lane produces.
    """
    if len(pits) < 4:
        return None
    boxes, lane = [], []
    for pos, yaw, _name in pits:
        fx, fz = math.sin(yaw), math.cos(yaw)
        lx, lz = pos[0] + fx * PIT_LANE_AHEAD, pos[2] + fz * PIT_LANE_AHEAD
        if not ground.on_road(lx, lz):
            lx, lz = pos[0] - fx * PIT_LANE_AHEAD, pos[2] - fz * PIT_LANE_AHEAD
            if not ground.on_road(lx, lz):
                continue
        boxes.append((pos[0], pos[1], pos[2], yaw))
        lane.append((lx, ground.height(lx, lz, pos[1]), lz, yaw))
    if len(lane) < 4:
        return None

    # Order them along the lane rather than by name, then walk the row.
    ax, az = lane[-1][0] - lane[0][0], lane[-1][2] - lane[0][2]
    n = math.hypot(ax, az) or 1.0
    ax, az = ax / n, az / n
    order = sorted(range(len(lane)), key=lambda i: lane[i][0] * ax + lane[i][2] * az)
    lane = [lane[i] for i in order]
    boxes = [boxes[i] for i in order]

    # Garages sit behind each box, away from the lane.
    garages = []
    for bx, by, bz, byaw in boxes:
        gx = bx - math.sin(byaw) * GARAGE_BACK
        gz = bz - math.cos(byaw) * GARAGE_BACK
        if not ground.on_road(gx, gz):
            gx, gz = bx, bz
        garages.append((gx, ground.height(gx, gz, by) if ground.on_road(gx, gz) else by,
                        gz, byaw))

    def nearest(p):
        return min(range(len(wps)),
                   key=lambda i: (wps[i][0] - p[0]) ** 2 + (wps[i][2] - p[2]) ** 2)

    entry, exit_ = nearest(lane[0]), nearest(lane[-1])
    # The row was ordered along its own axis, which may point against the
    # direction of travel. Cars would then drive the pit lane backwards.
    if (exit_ - entry) % len(wps) > len(wps) // 2:
        lane.reverse()
        boxes.reverse()
        garages.reverse()
        entry, exit_ = nearest(lane[0]), nearest(lane[-1])
    print("pit lane from AC: %d boxes, entry at wp %d, exit at wp %d"
          % (len(boxes), entry, exit_))
    return {"entry": entry, "exit": exit_, "lane": lane,
            "boxes": boxes, "garages": garages, "side": 1}


def build_pitlane(wps, ground):
    """Lay a pit lane alongside a stretch of the main path.

    AC's own pit dummies sit wherever that track put them, which need not be
    anywhere near the loop we generated, so the boxes are placed against our
    own pit lane instead. Everything is checked against the collidable
    surfaces, and the side with the most usable ground wins.
    """
    n = len(wps)
    run = max(8, min(24, n // 4))

    def lane_for(start, side):
        lane, boxes, garages = [], [], []
        for k in range(run + 1):
            i = (start + k) % n
            x, y, z, _half = wps[i]
            nx, _, nz, _ = wps[(i + 1) % n]
            fx, fz = nx - x, nz - z
            flen = math.hypot(fx, fz) or 1.0
            fx, fz = fx / flen, fz / flen
            px, pz = -fz * side, fx * side
            lx, lz = x + px * PIT_LANE_OFFSET, z + pz * PIT_LANE_OFFSET
            if not ground.on_road(lx, lz):
                return None
            lane.append((lx, ground.height(lx, lz, y), lz, math.atan2(fx, fz)))
            if 0 < k < run:
                bx = lx + px * PIT_BOX_OFFSET
                bz = lz + pz * PIT_BOX_OFFSET
                gx = bx + px * GARAGE_OFFSET
                gz = bz + pz * GARAGE_OFFSET
                if ground.on_road(bx, bz) and ground.on_road(gx, gz):
                    boxes.append((bx, ground.height(bx, bz, y), bz, math.atan2(fx, fz)))
                    garages.append((gx, ground.height(gx, gz, y), gz, math.atan2(fx, fz)))
        return lane, boxes, garages

    best = None
    for start in range(0, n, 2):
        for side in (1, -1):
            got = lane_for(start, side)
            if got and len(got[1]) >= 4:
                if best is None or len(got[1]) > len(best[1][1]):
                    best = (start, got, side)
    if best is None:
        return None
    start, (lane, boxes, garages), side = best
    print("pit lane: %d waypoints, %d boxes, side %s, from main wp %d"
          % (len(lane), len(boxes), "right" if side > 0 else "left", start))
    return {"entry": start, "exit": (start + run) % n, "lane": lane,
            "boxes": boxes, "garages": garages, "side": side}


def build(track_dir, out_path, layout):
    import convert_track
    # A real circuit is split across several model files; reading only the
    # first picks up whichever happens to sort first, often a prop file.
    paths, layout_root, _warn = convert_track.layout_files(track_dir, layout)
    placed = convert_track.placements(track_dir, layout)
    print("reading %d model files" % len(paths))
    ac_root = os.path.dirname(os.path.dirname(os.path.dirname(track_dir)))
    surf = convert_track.load_surfaces(layout_root, ac_root)
    valid = {key.upper() for key, info in surf.items() if info["valid"]}
    print("drivable surface keys: %s" % ", ".join(sorted(valid)))
    starts, pits, road, solid = [], [], [], []
    for path in paths:
        k = KN5(path, geometry=True, placement=placed.get(path))
        st, pt, rd, sd = collect(k, valid)
        starts += st
        pits += pt
        road += rd
        solid += sd
    starts.sort(key=lambda i: int(i[2].rsplit("_", 1)[-1])
                if i[2].rsplit("_", 1)[-1].isdigit() else 0)
    pits.sort(key=lambda i: int(i[2].rsplit("_", 1)[-1])
              if i[2].rsplit("_", 1)[-1].isdigit() else 0)

    # Mirror once, here, so the grid, pit lane, widths and sector polys are all
    # built in the same space the meshes are exported into.
    starts = [(mirror_pos(p), mirror_yaw(y), n) for p, y, n in starts]
    pits = [(mirror_pos(p), mirror_yaw(y), n) for p, y, n in pits]

    def flip(tris):
        return [(((-a[0], a[1]), (-b[0], b[1]), (-c[0], c[1])), ys)
                for (a, b, c), ys in tris]

    road = flip(road)
    solid = flip(solid)
    if not road:
        raise SystemExit("no ROAD meshes found, cannot fit a path")

    ac_line = [mirror_pos(p) for p in read_fast_lane(layout_root)]
    if ac_line:
        pts, length = resample_line(ac_line, SPACING)
        grid = RoadGrid(road)
        dist = grid.clearance()
        grid.dist = dist
        wps = []
        for x, y, z in pts:
            gi = grid.gz(z) * grid.w + grid.gx(x)
            half = dist[gi] * grid.cell if 0 <= gi < len(dist) else 6.0
            wps.append((x, y, z, half))
        on = sum(1 for x, _y, z, _h in wps if grid.on_road(x, z))
        print("using AC's racing line: %d points -> %d waypoints, %.0f m, "
              "%d on road" % (len(ac_line), len(wps), length, on))
    else:
        loop, grid = fit_loop(road, seed=starts[0][0] if starts else None)
        wps, length = resample(loop)
    n = len(wps)
    print("grid %d, pits %d, road triangles %d" % (len(starts), len(pits), len(road)))
    print("path %d waypoints, %.1f m around" % (n, length))

    ground = RoadGrid(solid)
    probe = SurfaceProbe(solid)
    ground.probe = probe

    # AC's start and pit markers sit at the centre of a marker box, typically a
    # couple of metres up, so cars spawn in mid air and fall. Drop each one onto
    # the surface directly underneath it -- the first one down, not the cell's
    # average, which on stacked ground lies between the layers.
    def on_ground(items):
        out = []
        for pos, yaw, name in items:
            y = pos[1]
            h = probe.below(pos[0], pos[1], pos[2])
            if h is not None:
                y = h + 0.05
            out.append(((pos[0], y, pos[2]), yaw, name))
        return out

    before = starts[0][0][1] if starts else 0.0
    starts = on_ground(starts)
    pits = on_ground(pits)
    if starts:
        print("grid height %.2f -> %.2f (dropped onto the surface)"
              % (before, starts[0][0][1]))

    pl = pitlane_from_ac(pits, wps, ground) or build_pitlane(wps, ground)
    if pl:
        pit_boxes, pit_garages = pl["boxes"], pl["garages"]
    else:
        print("!! no pit lane could be placed, falling back to AC pit dummies")
        pit_boxes = [(p[0], p[1], p[2], y) for p, y, _nm in pits]
        pit_garages = pit_boxes
    total_wp = len(wps) + (len(pl["lane"]) + 2 * len(pl["boxes"]) if pl else 0)

    L = []
    a = L.append
    a("//[[gMa1.002f (c)2015    ]] [[ generated from Assetto Corsa ]]")
    a("[Features]")
    a("pitlanes=%d" % (1 if pl else 0))
    a("startinggrid=%d" % max(1, len(starts)))
    a("pitspots=%d" % max(1, len(pit_boxes)))
    a("garagespots=1")
    a("auxspots=0")
    a("acceptabledriverlinenoise=1.000000")
    a("StartingStretch=20.000000")
    for pname in ("FASTEST", "LEFT", "RIGHT"):
        a("definepath=%s" % pname)
        a("pathtime=%.4f" % (length / 25.0))
    a("")

    def spots(tag, items):
        a("[%s]" % tag)
        for i, (pos, yaw, _name) in enumerate(items):
            a("GridIndex=%d" % i)
            a("Pos=(%.3f,%.3f,%.3f)" % pos)
            a("Ori=(0.000,%.3f,0.000)" % yaw)
        a("")

    spots("GRID", starts)
    spots("ALTGRID", starts)
    spots("TELEPORT", starts)

    # Every car needs a pit box and a garage on a real pit lane. Without them
    # rF2 red flags the whole field for "Exiting Pits Under Red" and aborts on
    # entering the world.
    a("[PITS]")
    for i, (bx, by, bz, byaw) in enumerate(pit_boxes):
        gx, gy, gz, gyaw = pit_garages[i]
        a("TeamIndex=%d" % i)
        a("PitPos=(%.3f,%.3f,%.3f)" % (bx, by, bz))
        a("PitOri=(0.000,%.3f,0.000)" % face_out(byaw))
        a("GarPos=(0,%.3f,%.3f,%.3f)" % (gx, gy, gz))
        a("GarOri=(0,0.000,%.3f,0.000)" % face_out(gyaw))
    a("")

    a("[Waypoint]")
    a("trackstate=4507")
    a("drivinglines=1")
    a("autogengridf=(0.0,11.00)")
    a("autogenaltgridf=(1.0,8.00)")
    a("number_waypoints=%d" % total_wp)
    if pl:
        a("teleportwp=(%d)" % pl["entry"])
        a("pitlanepaths=(1,1)")
    a("lap_length=%.6f" % length)
    a("sector_1_length=%.6f" % (length / SECTORS))
    a("sector_2_length=%.6f" % (length * 2 / SECTORS))
    a("LeftHandedPits=%d" % (1 if pl and pl["side"] < 0 else 0))
    a("AIBrakingStiffness=(1.0000,1.0000,0.9000)")
    a("LaneSpacing=4.50")
    a("AIRange=(0.1000)")
    a("AISpec=(0.0000,0.0000,1.0000,0.0000)")

    dist = 0.0
    for i in range(n):
        x, y, z, half = wps[i]
        nx, _, nz, _ = wps[(i + 1) % n]
        fx, fz = nx - x, nz - z
        flen = math.hypot(fx, fz) or 1.0
        fx, fz = fx / flen, fz / flen
        # normal is up on a flat surface, perp is normal x forward
        px, pz = -fz, fx
        yaw = math.atan2(fx, fz)
        half = max(4.0, min(half, 15.0))

        a("wp_pos=(%.4f,%.4f,%.4f)" % (x, y, z))
        a("wp_perp=(%.4f,0.0000,%.4f)" % (px, pz))
        a("wp_normal=(0.0000,1.0000,0.0000)")
        # rF2 wants three driving lines per waypoint. With one it logs
        # "less than 3 paths defined" and patches in defaults.
        lane = min(2.0, half * 0.4)
        for pi, off in ((0, 0.0), (1, -lane), (2, lane)):
            a("wp_pathinfo2=(%d,%.4f,-1.0000,-1.0000)" % (pi, off))
            a("wp_oriantation=(%d,0.0000,%.4f,0.0000)" % (pi, yaw))
            a("wp_pathflags=(%d,0)" % pi)
        a("wp_width=(%.3f,%.3f,%.3f,%.3f)" % (half, half, half + 2, half + 2))
        a("wp_dwidth=(%.3f,%.3f,0.000,0.000)" % (half + 2, half + 2))
        a("wp_lockedAlpha=(0)")
        a("wp_test_speed=(25.000000)")
        a("wp_reverb=(0)")
        sector = min(SECTORS - 1, int(dist / (length / SECTORS)))
        a("wp_score=(%d,%.3f)" % (sector, dist))
        a("wp_wpse=(0,0)")
        a("wp_branchID=(0)")
        a("wp_bitfields=(0)")
        a("wp_pitlane=(%d)" % (1 if pl and i == pl["entry"] else 0))
        if pl and i == pl["entry"]:
            a("WP_PTRS=(%d,%d,%d,1)" % ((i - 1) % n, (i + 1) % n, n))
        else:
            a("WP_PTRS=(%d,%d,-1,0)" % ((i - 1) % n, (i + 1) % n))
        dist += math.hypot(nx - x, nz - z)

    # pit lane, as branch 1: it leaves the main path at the entry waypoint and
    # rejoins it at the exit
    if pl:
        lane = pl["lane"]
        m = len(lane)
        for j, (x, y, z, yaw) in enumerate(lane):
            fx, fz = math.sin(yaw), math.cos(yaw)
            px, pz = -fz, fx
            prev = pl["entry"] if j == 0 else n + j - 1
            nxt = pl["exit"] if j == m - 1 else n + j + 1
            a("wp_pos=(%.4f,%.4f,%.4f)" % (x, y, z))
            a("wp_perp=(%.4f,0.0000,%.4f)" % (px, pz))
            a("wp_normal=(0.0000,1.0000,0.0000)")
            for pi, off in ((0, 0.0), (1, -1.5), (2, 1.5)):
                a("wp_pathinfo2=(%d,%.4f,-1.0000,-1.0000)" % (pi, off))
                a("wp_oriantation=(%d,0.0000,%.4f,0.0000)" % (pi, yaw))
                a("wp_pathflags=(%d,0)" % pi)
            a("wp_width=(4.000,4.000,6.000,6.000)")
            a("wp_dwidth=(6.000,6.000,0.000,0.000)")
            a("wp_lockedAlpha=(0)")
            a("wp_test_speed=(-1.000000)")
            a("wp_reverb=(0)")
            a("wp_score=(0,%.3f)" % (j * 5.0))
            a("wp_wpse=(0,0)")
            a("wp_branchID=(1)")
            a("wp_bitfields=(0)")
            a("wp_pitlane=(1)")
            a("WP_PTRS=(%d,%d,-1,0)" % (prev, nxt))

        # Pit boxes need waypoints of their own, separate from the [PITS] list.
        # Without them rF2 logs "No pit box waypoints found", cannot seat the
        # cars, and aborts. Each box is a two waypoint stub hanging off the
        # nearest pit lane point: an approach, then the box itself.
        base = n + m
        for bi, (bx, by, bz, byaw) in enumerate(pl["boxes"]):
            near = min(range(m), key=lambda j: (lane[j][0] - bx) ** 2
                                               + (lane[j][2] - bz) ** 2)
            lane_idx = n + near
            appr = base + bi * 2
            box = appr + 1
            ax = (lane[near][0] + bx) / 2.0
            az = (lane[near][2] + bz) / 2.0
            for idx, (px_, py_, pz_, ptrs) in (
                    (appr, (ax, by, az, (lane_idx, box))),
                    (box, (bx, by, bz, (appr, box)))):
                fx, fz = math.sin(byaw), math.cos(byaw)
                a("wp_pos=(%.4f,%.4f,%.4f)" % (px_, py_, pz_))
                a("wp_perp=(%.4f,0.0000,%.4f)" % (-fz, fx))
                a("wp_normal=(0.0000,1.0000,0.0000)")
                for pi in (0, 1, 2):
                    a("wp_pathinfo2=(%d,0.0000,-1.0000,-1.0000)" % pi)
                    a("wp_oriantation=(%d,0.0000,%.4f,0.0000)" % (pi, byaw))
                    a("wp_pathflags=(%d,0)" % pi)
                a("wp_width=(3.000,3.000,3.000,3.000)")
                a("wp_dwidth=(3.000,3.000,0.000,0.000)")
                a("wp_lockedAlpha=(0)")
                a("wp_test_speed=(0.000000)")
                a("wp_reverb=(0)")
                a("wp_score=(0,0.000)")
                a("wp_wpse=(0,0)")
                a("wp_branchID=(%d)" % bi)
                a("wp_bitfields=(1)")
                a("wp_pitlane=(1)")
                a("WP_PTRS=(%d,%d,-1,%d)" % (ptrs[0], ptrs[1], bi))

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("wrote %s" % out_path)
    write_sector_polys(wps, os.path.join(os.path.dirname(out_path), "sectors.json"))
    return {"waypoints": n, "length": length, "grid": len(starts), "pits": len(pits)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("track_dir")
    ap.add_argument("out_path")
    ap.add_argument("--layout", default=None)
    args = ap.parse_args()
    build(args.track_dir, args.out_path, args.layout)
