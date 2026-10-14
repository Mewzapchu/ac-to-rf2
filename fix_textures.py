"""Convert AC textures into formats a DX11 renderer can actually create.

Assetto Corsa ships some textures in legacy D3D9 pixel formats: 24 bit RGB and
8 bit luminance-alpha. Neither exists in DX11, so rF2 cannot make a texture out
of them and dies when the renderer starts, with nothing written to the log.
Anything not already block compressed is re-encoded to BC3 with mipmaps.

AC also caps its mip chains at five levels whatever the texture size, so a
4096x1024 road stops at 256x64. Asked for anything smaller the hardware has to
clamp to that last level, which is what makes distant tarmac and terrain
sparkle. Those get rebuilt with a full chain, each in its own format.
"""
import os
import shutil
import struct
import argparse
import subprocess

TEXCONV = (r"C:\Program Files (x86)\Steam\steamapps\common\rFactor 2"
           r"\Support\Tools\MapConverter\texconv.exe")
BLOCK = (b"DXT1", b"DXT3", b"DXT5", b"BC4U", b"BC5U", b"ATI1", b"ATI2")

# Rebuilding a block compressed texture means decoding and recompressing it, so
# it is only worth doing in its own format: turning a normal map into BC3 would
# cost far more than the missing mips are worth.
SAME_FORMAT = {b"DXT1": "BC1_UNORM", b"DXT3": "BC2_UNORM", b"DXT5": "BC3_UNORM",
               b"BC4U": "BC4_UNORM", b"ATI1": "BC4_UNORM",
               b"BC5U": "BC5_UNORM", b"ATI2": "BC5_UNORM"}

# Below this a short chain costs nothing to look at and recompressing only adds
# error, so small masks and markers are left exactly as AC shipped them.
MIP_MIN = 128

# Building mips, texconv weights colour by alpha unless told to treat the two
# separately. AC stores plenty of opaque surfaces with a zero alpha channel, and
# without this their smaller mips come out black.


def full_levels(w, h):
    """How many mip levels a texture of this size needs to reach 1x1."""
    return max(w, h).bit_length()


# AC loads whatever image is inside a .dds name; mod tracks ripped from other
# games often ship PNGs or JPEGs that way. rF2 reads only real DDS and draws
# the rest flat blue (ek_nagao: every texture is a PNG).
IMAGE_MAGIC = ((b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"),
               (b"BM", ".bmp"), (b"GIF8", ".gif"))


def image_kind(path):
    """The real extension of a non-DDS image hiding behind a .dds name."""
    with open(path, "rb") as f:
        head = f.read(8)
    for magic, ext in IMAGE_MAGIC:
        if head.startswith(magic):
            return ext
    return None


def describe(path):
    with open(path, "rb") as f:
        h = f.read(128)
    if len(h) < 128 or h[:4] != b"DDS ":
        return None
    height, width = struct.unpack_from("<II", h, 12)
    fourcc = h[84:88]
    bpp = struct.unpack_from("<I", h, 88)[0]
    mips = struct.unpack_from("<I", h, 28)[0]
    return width, height, fourcc, bpp, mips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tex_dir")
    ap.add_argument("--texconv", default=TEXCONV)
    ap.add_argument("--format", default="BC3_UNORM",
                    help="format for textures DX11 cannot load at all")
    ap.add_argument("--keep-mips", action="store_true",
                    help="leave AC's truncated mip chains alone")
    args = ap.parse_args()

    tex_dir = os.path.abspath(args.tex_dir)
    tmp = os.path.join(os.path.dirname(tex_dir.rstrip("\/")), "_converted")
    os.makedirs(tmp, exist_ok=True)

    todo = []
    for f in sorted(os.listdir(tex_dir)):
        if not f.lower().endswith(".dds"):
            continue
        info = describe(os.path.join(tex_dir, f))
        if info is None:
            kind = image_kind(os.path.join(tex_dir, f))
            if kind:
                todo.append((f, 0, 0, 0, args.format, kind[1:] + " named .dds"))
            else:
                print("skip (not dds): %s" % f)
            continue
        w, h, fourcc, bpp, mips = info
        if fourcc not in BLOCK:
            todo.append((f, w, h, bpp, args.format, "%dbpp" % bpp))
        elif (not args.keep_mips and fourcc in SAME_FORMAT
              and max(w, h) >= MIP_MIN and mips < full_levels(w, h)):
            todo.append((f, w, h, bpp, SAME_FORMAT[fourcc],
                         "%d of %d mips" % (mips, full_levels(w, h))))

    if not todo:
        print("nothing to convert")
        return

    print("re-encoding %d textures" % len(todo))
    converted = failed = 0
    for f, w, h, bpp, fmt, why in todo:
        src = f
        if why.endswith("named .dds"):
            # texconv picks its decoder by extension: give it the real one.
            src = os.path.join(tmp, os.path.splitext(f)[0] + "." + why.split()[0])
            shutil.copyfile(os.path.join(tex_dir, f), src)
        r = subprocess.run(
            [args.texconv, "-f", fmt, "-m", "0", "-sepalpha", "-y", "-ft", "dds",
             "-o", tmp, src],
            cwd=tex_dir, capture_output=True, text=True)
        out = None
        for cand in (f, os.path.splitext(f)[0] + ".DDS",
                     os.path.splitext(f)[0] + ".dds"):
            p = os.path.join(tmp, cand)
            if os.path.exists(p):
                out = p
                break
        if r.returncode != 0 or out is None:
            print("  FAILED %-28s %s" % (f, (r.stdout or r.stderr).strip()[-70:]))
            failed += 1
            continue
        shutil.move(out, os.path.join(tex_dir, f))
        print("  %-28s %dx%d %-14s -> %s" % (f, w, h, why, fmt))
        converted += 1

    shutil.rmtree(tmp, ignore_errors=True)
    print("converted %d, failed %d" % (converted, failed))


if __name__ == "__main__":
    main()
