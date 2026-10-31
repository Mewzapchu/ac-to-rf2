"""Convert one Assetto Corsa track into rFactor 2, end to end.

    python build_track.py "<AC track folder>" [--install "<rF2>\\ModDev\\Locations"]

Stages, in order:
  1. read the model, map surfaces, extract textures      convert_track.py
  2. re-encode textures DX11 cannot load                 fix_textures.py
  3. flatten AC multilayer materials into one texture    bake_layers.py
  4. waypoints, grid, pit lane, timing strips            make_aiw.py
  5. trackside cameras                                   make_cam.py
  6. geometry to rF2 meshes                              blender_stage.py (Blender)
  7. scene, database, terrain, install                   finalize_track.py
"""
import os
import sys
import json
import shutil
import struct
import argparse
import subprocess

from parallel import default_jobs

HERE = os.path.dirname(os.path.abspath(__file__))
BLENDER = r"C:\Program Files\Blender Foundation\Blender 3.6\blender.exe"


def step(n, title):
    print("\n[%d/7] %s" % (n, title))
    sys.stdout.flush()


WARNINGS = []


def collect_warnings(out):
    # Collected for the end of the build, where they cannot scroll away.
    WARNINGS.extend(l.strip() for l in out.splitlines()
                    if l.strip().startswith(("WARNING", "!! no file produced")))


def run(args, quiet=False):
    r = subprocess.run(args, capture_output=True, text=True, cwd=HERE)
    out = (r.stdout or "") + (r.stderr or "")
    collect_warnings(out)
    if r.returncode != 0:
        print(out[-2000:])
        raise SystemExit("failed: %s" % " ".join(str(a) for a in args[:3]))
    if not quiet:
        for line in out.splitlines():
            if line.strip() and not line.startswith(("Blender", "TBBmalloc", "Read prefs")):
                print("   " + line[:160])
    return out


def write_solid_texture(path, rgb):
    """A 4x4 block of one colour, for a shader slot AC gives us nothing for.

    rF2's multi texture shaders declare every stage as required, and the
    exporter writes the slot's own name where the filename should go if it is
    left empty. rF2 then hunts for a texture called "sBump" and fails the whole
    scene. A neutral stand-in costs 8 bytes and cannot do that.
    """
    r, g, b = rgb
    c = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    h = bytearray(128)
    h[0:4] = b"DDS "
    struct.pack_into("<I", h, 4, 124)
    struct.pack_into("<I", h, 8, 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000)
    struct.pack_into("<I", h, 12, 4)
    struct.pack_into("<I", h, 16, 4)
    struct.pack_into("<I", h, 20, 8)
    struct.pack_into("<I", h, 28, 1)
    struct.pack_into("<I", h, 76, 32)
    struct.pack_into("<I", h, 80, 0x4)
    h[84:88] = b"DXT1"
    struct.pack_into("<I", h, 108, 0x1000)
    with open(path, "wb") as f:
        f.write(bytes(h))
        f.write(struct.pack("<HHI", c, c, 0))


def write_clear_texture(path):
    """A 4x4 fully transparent block, used by the timing strips."""
    h = bytearray(128)
    h[0:4] = b"DDS "
    struct.pack_into("<I", h, 4, 124)
    struct.pack_into("<I", h, 8, 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000)
    struct.pack_into("<I", h, 12, 4)
    struct.pack_into("<I", h, 16, 4)
    struct.pack_into("<I", h, 20, 16)
    struct.pack_into("<I", h, 28, 1)
    struct.pack_into("<I", h, 76, 32)
    struct.pack_into("<I", h, 80, 0x4)
    h[84:88] = b"DXT5"
    struct.pack_into("<I", h, 108, 0x1000)
    with open(path, "wb") as f:
        f.write(bytes(h))
        f.write(bytes(16))


GEOMETRY_LINES = ("EXPORTED", "backdrop", "materials prepared", "!!")


def blender_stage(pre, post, gmt_dir, jobs):
    """Run the geometry stage, split across `jobs` Blenders when above 1.

    Blender runs its scripts, the rF2 exporter included, on one core, and
    each mesh is exported to a gmt of its own. So the meshes are shared out:
    every Blender walks the whole model (names and order stay those of a
    single run) and exports only its share, into a folder of its own -- the
    stage finds what the exporter wrote by looking at its output folder,
    which only works if nobody else writes there. The shares are then moved
    back into one folder and listed in single-run order.

    The Blender command line is pre + [output folder] + post.
    """
    if jobs <= 1:
        log = run(pre + [gmt_dir] + post, quiet=True)
        for line in log.splitlines():
            if line.startswith(GEOMETRY_LINES):
                print("   " + line[:160])
        return log

    shard_dirs = [os.path.join(gmt_dir, "_shard%d" % i) for i in range(jobs)]
    procs = []
    for i, d in enumerate(shard_dirs):
        os.makedirs(d)
        logf = open(os.path.join(d, "blender.log"), "w")
        procs.append((subprocess.Popen(
            pre + [d] + post + ["--shard", "%d/%d" % (i, jobs)],
            stdout=logf, stderr=subprocess.STDOUT, text=True, cwd=HERE), logf))
    print("   %d Blenders side by side" % jobs)
    for p, logf in procs:
        p.wait()
        logf.close()

    exported, logs, printed = [], [], set()
    for i, d in enumerate(shard_dirs):
        log = open(os.path.join(d, "blender.log"), errors="replace").read()
        logs.append(log)
        collect_warnings(log)
        record = os.path.join(d, "exported.json")
        # Blender can fail without a non-zero exit code; a missing record is
        # the sign, as for a single run.
        if procs[i][0].returncode != 0 or not os.path.exists(record):
            print(log[-2500:])
            raise SystemExit("geometry stage: Blender %d of %d failed (log above, "
                             "and in %s)" % (i + 1, jobs, d))
        for line in log.splitlines():
            # Every shard prepares every material, so their messages repeat.
            if line.startswith(GEOMETRY_LINES) and not line.startswith("EXPORTED") \
                    and line not in printed:
                printed.add(line)
                print("   " + line[:160])
        for e in json.load(open(record)):
            e["_dir"] = d
            exported.append(e)

    exported.sort(key=lambda e: e["_seq"])
    merged, names = [], set()
    for e in exported:
        src = os.path.join(e.pop("_dir"), e["gmt"])
        del e["_seq"]
        # A single run would have had the exporter rename or overwrite a
        # clash. None has been seen (six tracks); say so loudly if one is.
        if e["gmt"].lower() in names:
            msg = "WARNING two meshes exported as %s; kept the first" % e["gmt"]
            print("   " + msg)
            WARNINGS.append(msg)
            continue
        names.add(e["gmt"].lower())
        os.replace(src, os.path.join(gmt_dir, e["gmt"]))
        merged.append(e)
    with open(os.path.join(gmt_dir, "exported.json"), "w") as f:
        json.dump(merged, f, indent=1)
    for d in shard_dirs:
        shutil.rmtree(d, ignore_errors=True)
    print("   EXPORTED %d (from %d Blenders)" % (len(merged), jobs))
    return "\n".join(logs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("track_dir")
    ap.add_argument("--install", default=None,
                    help='an rF2 ModDev\\Locations folder, or "auto" to find it '
                         'in your Steam libraries')
    ap.add_argument("--hour", default="12:00")
    ap.add_argument("--bake-size", type=int, default=12288)
    ap.add_argument("--grain", default="2")
    ap.add_argument("--detail-metres", default="2.0",
                    help="how often rF2 repeats a second stage detail texture")
    ap.add_argument("--gain", default="1.0",
                    help="lifts AC's lighting coefficients to rF2's light level")
    ap.add_argument("--diffuse", default="0.42")
    ap.add_argument("--ambient", default="0.30")
    ap.add_argument("--scene-ambient", default="126")
    ap.add_argument("--blender", default=BLENDER)
    ap.add_argument("--layout", default=None,
                    help="for a multi-layout track: the part after models_ in models_<layout>.ini")
    ap.add_argument("--skip-geometry", action="store_true",
                    help="reuse the meshes already exported")
    ap.add_argument("--jobs", type=int, default=default_jobs(),
                    help="Blenders and texture conversions run side by side "
                         "(default %d here; 1 runs everything one at a time, as "
                         "before)" % default_jobs())
    args = ap.parse_args()
    if args.install == "auto":
        import steam_paths
        args.install = steam_paths.LOCATIONS

    track = os.path.basename(os.path.abspath(args.track_dir).rstrip("\\/"))
    if args.layout:
        track = "%s_%s" % (track, args.layout)
    out = os.path.join(HERE, "out", track)
    py = sys.executable
    lay = ["--layout", args.layout] if args.layout else []
    print("converting %s" % track)

    step(1, "model, surfaces and textures")
    run([py, "convert_track.py", args.track_dir, "--textures"] + lay)

    step(2, "texture formats")
    run([py, "fix_textures.py", os.path.join(out, "textures"), "--jobs", str(args.jobs)])
    tex = os.path.join(out, "textures")
    write_clear_texture(os.path.join(tex, "_invisible.dds"))
    # Black. White was the neutral value if rF2 multiplied this by
    # Specular_Color, but the eight materials that got it -- tree bark, wooden
    # posts, the fuel station -- came out white, so the shader is reading the
    # map far more directly than that. AC gives these a specular between 0.00
    # and 0.15, so losing it costs nothing next to a white sheen.
    write_solid_texture(os.path.join(tex, "_flat_spec.dds"), (0, 0, 0))
    write_solid_texture(os.path.join(tex, "_flat_nm.dds"), (128, 128, 255))
    write_solid_texture(os.path.join(tex, "_white.dds"), (255, 255, 255))

    step(3, "composing multilayer materials")
    run([py, "bake_layers.py", out, "--size", str(args.bake_size),
         "--grain", args.grain, "--detail-metres", args.detail_metres,
         "--jobs", str(args.jobs)])

    step(4, "waypoints, grid and pit lane")
    run([py, "make_aiw.py", args.track_dir, os.path.join(out, track + ".AIW")] + lay)

    step(5, "trackside cameras")
    run([py, "make_cam.py", args.track_dir, os.path.join(out, track + ".cam"),
         "--aiw", os.path.join(out, track + ".AIW")] + lay)

    step(6, "geometry (this is the slow one)")
    gmt_dir = os.path.join(out, "gmt")
    record = os.path.join(gmt_dir, "exported.json")
    if args.skip_geometry and os.path.isdir(gmt_dir):
        print("   reusing existing meshes")
    else:
        # Start from empty. Meshes left from a previous run would otherwise be
        # installed alongside a scene file that does not reference them.
        if os.path.isdir(gmt_dir):
            shutil.rmtree(gmt_dir)
        log = blender_stage(
            [args.blender, "--background", "--factory-startup", "--python",
             "blender_stage.py", "--", os.path.join(out, "material_map.json")],
            ["--diffuse", args.diffuse, "--ambient", args.ambient, "--gain", args.gain],
            gmt_dir, args.jobs)

    # Blender can fail without a non-zero exit code, so check the result rather
    # than trusting it: an empty record means nothing was exported.
    try:
        exported = json.load(open(record))
    except Exception:
        exported = []
    if not exported:
        print(log[-2500:] if not args.skip_geometry else "")
        raise SystemExit("geometry stage produced nothing; see the output above")
    print("   %d meshes exported" % len(exported))

    step(7, "scene, database and install")
    cmd = [py, "finalize_track.py", out, "--hour", args.hour,
           "--ambient", args.scene_ambient]
    if args.install:
        cmd += ["--install", args.install]
    run(cmd)

    if args.install:
        lay = os.path.join(args.install, track, track)
        for f in os.listdir(lay) if os.path.isdir(lay) else []:
            if f.lower().endswith((".cbash", ".rrshd")):
                os.remove(os.path.join(lay, f))
    if WARNINGS:
        print("\n%d warning(s) -- read these before loading the track:" % len(set(WARNINGS)))
        for w in dict.fromkeys(WARNINGS):
            print("   " + w[:200])
    print("\n%s ready" % track)


if __name__ == "__main__":
    main()
