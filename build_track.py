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

HERE = os.path.dirname(os.path.abspath(__file__))
BLENDER = r"C:\Program Files\Blender Foundation\Blender 3.6\blender.exe"


def step(n, title):
    print("\n[%d/7] %s" % (n, title))
    sys.stdout.flush()


WARNINGS = []


def run(args, quiet=False):
    r = subprocess.run(args, capture_output=True, text=True, cwd=HERE)
    out = (r.stdout or "") + (r.stderr or "")
    # Collected for the end of the build, where they cannot scroll away.
    WARNINGS.extend(l.strip() for l in out.splitlines()
                    if l.strip().startswith(("WARNING", "!! no file produced")))
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("track_dir")
    ap.add_argument("--install", default=None)
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
    ap.add_argument("--zip", action="store_true",
                    help="also write <track>_rf2.zip, ready to unzip into ModDev/Locations")
    ap.add_argument("--skip-geometry", action="store_true",
                    help="reuse the meshes already exported")
    args = ap.parse_args()

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
    run([py, "fix_textures.py", os.path.join(out, "textures")])
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
         "--grain", args.grain, "--detail-metres", args.detail_metres])

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
        log = run([args.blender, "--background", "--factory-startup", "--python",
                   "blender_stage.py", "--", os.path.join(out, "material_map.json"),
                   gmt_dir, "--diffuse", args.diffuse,
                   "--ambient", args.ambient, "--gain", args.gain], quiet=True)
        for line in log.splitlines():
            if line.startswith(("EXPORTED", "backdrop", "materials prepared", "!!")):
                print("   " + line[:160])

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
    if args.zip:
        # The staged location, exactly as installed, as one file: unzip it
        # into rFactor 2/ModDev/Locations on any machine and it appears there
        # as <track>. Cache files rF2 writes on first load are left out.
        import zipfile
        stage = os.path.join(out, "rf2", track)
        zpath = os.path.join(out, track + "_rf2.zip")
        if os.path.isdir(stage):
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                for root, _dirs, names in os.walk(stage):
                    for nm in names:
                        if nm.lower().endswith((".cbash", ".rrshd")):
                            continue
                        full = os.path.join(root, nm)
                        z.write(full, os.path.join(track, os.path.relpath(full, stage)))
            print("   zip -> %s (%.0f MB)" % (zpath, os.path.getsize(zpath) / 1e6))
        else:
            print("   !! no staged location at %s, nothing to zip" % stage)
    if WARNINGS:
        print("\n%d warning(s) -- read these before loading the track:" % len(set(WARNINGS)))
        for w in dict.fromkeys(WARNINGS):
            print("   " + w[:200])
    print("\n%s ready" % track)


if __name__ == "__main__":
    main()
