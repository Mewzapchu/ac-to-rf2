"""Convert Assetto Corsa trackside cameras into an rFactor 2 .cam file.

AC stores each camera as a position plus forward and up vectors across a set of
cameras*.ini files. rF2 wants a position, an orientation as pitch/yaw/roll, and
an activation point with a radius that decides when that camera takes over.

Activation points are taken from the racing line: each camera claims the
waypoint it is closest to, and the radius is sized so the claims tile the lap.
"""
import os
import re
import math
import glob
import argparse
import configparser

HEAD = "//[[gMa1.002f (c)2015    ]] [[ converted from Assetto Corsa ]]\n"

BODY = """TrackingCam={name}
{{
  Fov=({fov_min:.6f}, {fov_max:.6f})
  Clear=FALSE
  Color=(0, 0, 0)
  ClipPlanes=({near:.6f}, {far:.6f})
  LODMultiplier=(6.000000)
  Size=(1.000000, 1.000000)
  Center=(0.500000, 0.500000)
  MipmapLODBias=(0.000000)
  Flags1=(1)
  Flags2=(0)
  SoundName=""
  SoundParams=(1.000,1,15)
  MinShadowRange=(0.100)
  MaxShadowRange=(450.000)
  ShadowSplitRatio=(0.920)
  ShadowParams=(0.000050,1.000,100.000)
  ShadowParams=(0.000050,1.000,100.000)
  ShadowParams=(0.000050,1.000,100.000)
  ShadowParams=(0.000050,1.000,100.000)
  ShadowParams=(0.000050,1.000,100.000)
  ShadowParams=(0.000050,1.000,100.000)
  ShadowParams=(0.000050,1.000,0.000)
  ShadowParams=(0.000000,0.000,0.000)
  Position=({px:.6f}, {py:.6f}, {pz:.6f})
  Orientation=({pitch:.6f}, {yaw:.6f}, {roll:.6f})
  ActivationLocation=({ax:.6f}, {ay:.6f}, {az:.6f})
  ActivationRadius=({radius:.6f})
}}

"""


def vec(s):
    return [float(v) for v in s.replace(",", " ").split()]


def read_cameras(data_dir):
    out = []
    for path in sorted(glob.glob(os.path.join(data_dir, "cameras*.ini"))):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            cp.read_file(f)
        for sec in cp.sections():
            if not sec.startswith("CAMERA_"):
                continue
            c = cp[sec]
            if "POSITION" not in c or "FORWARD" not in c:
                continue
            # mirrored to match the geometry, which flips handedness
            pos = vec(c["POSITION"])
            fwd = vec(c["FORWARD"])
            up = vec(c.get("UP", "0,1,0"))
            out.append({
                "pos": [-pos[0], pos[1], pos[2]],
                "fwd": [-fwd[0], fwd[1], fwd[2]],
                "up": [-up[0], up[1], up[2]],
                "fov_min": float(c.get("MIN_FOV", 30) or 30),
                "fov_max": float(c.get("MAX_FOV", 30) or 30),
                "near": float(c.get("NEAR_PLANE", 1.05) or 1.05),
                "far": float(c.get("FAR_PLANE", 3000) or 3000),
                "src": "%s/%s" % (os.path.basename(path), sec),
            })
    return out


# Measured from the stock track: the camera file stores a world point (x,y,z)
# as (x, -y, -z). Activation points there are the exact negative of the track's
# own Y and Z, while X matches.
def to_cam_space(p):
    return (p[0], -p[1], -p[2])


def orientation(fwd, up):
    """pitch/yaw/roll in radians from AC's forward and up vectors."""
    fx, fy, fz = fwd
    n = math.sqrt(fx * fx + fy * fy + fz * fz) or 1.0
    fx, fy, fz = fx / n, fy / n, fz / n
    yaw = math.atan2(fx, fz)
    pitch = math.asin(max(-1.0, min(1.0, -fy)))
    # roll is the tilt of the up vector around the forward axis
    rx = math.cos(yaw)
    rz = -math.sin(yaw)
    roll = math.atan2(up[0] * rx + up[2] * rz, up[1])
    return pitch, yaw, roll


def waypoints(aiw_path):
    if not aiw_path or not os.path.exists(aiw_path):
        return []
    d = open(aiw_path, "r", encoding="utf-8", errors="replace").read()
    return [tuple(float(v) for v in m.groups())
            for m in re.finditer(
                r"wp_pos=\(([-\d.]+),([-\d.]+),([-\d.]+)\)", d)]


def build(track_dir, out_path, aiw_path=None, layout=None):
    root = os.path.join(track_dir, layout) if layout else track_dir
    cams = read_cameras(os.path.join(root, "data"))
    if not cams:
        print("no AC cameras found")
        return 0
    wps = waypoints(aiw_path)
    lap = 0.0
    for i in range(len(wps)):
        a, b = wps[i], wps[(i + 1) % len(wps)]
        lap += math.hypot(b[0] - a[0], b[2] - a[2])
    # spread the cameras over the lap so each covers its share of it
    radius = max(40.0, lap / max(1, len(cams)) * 0.75) if lap else 150.0

    text = [HEAD]
    for i, c in enumerate(cams):
        px, py, pz = to_cam_space(c["pos"])
        fwd = to_cam_space(c["fwd"])
        up = to_cam_space(c["up"])
        pitch, yaw, roll = orientation(fwd, up)
        if wps:
            # aim the activation point at the nearest bit of track
            near = min(wps, key=lambda w: (w[0] - c["pos"][0]) ** 2
                                          + (w[2] - c["pos"][2]) ** 2)
            ax, ay, az = to_cam_space(near)
        else:
            ax, ay, az = px, py, pz
        text.append(BODY.format(
            name="TRACKING%03d" % i,
            fov_min=38.0,
            fov_max=max(12.0, min(60.0, min(c["fov_min"], c["fov_max"]))),
            near=c["near"], far=c["far"],
            px=px, py=py, pz=pz,
            pitch=pitch, yaw=yaw, roll=roll,
            ax=ax, ay=ay, az=az, radius=radius))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(text))
    print("wrote %d cameras -> %s" % (len(cams), out_path))
    return len(cams)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("track_dir")
    ap.add_argument("out_path")
    ap.add_argument("--aiw", default=None)
    ap.add_argument("--layout", default=None)
    args = ap.parse_args()
    build(args.track_dir, args.out_path, args.aiw, args.layout)
