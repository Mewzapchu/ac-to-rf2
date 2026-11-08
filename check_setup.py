"""Check that everything a conversion needs is in place, before it starts.

    python check_setup.py ["<AC track folder>"] [--layout <name>]
                          [--blender <path>] [--rf2 <folder>] [--ac <folder>]

build_track.py runs the same checks first, so a missing piece is reported in a
few seconds, in plain words, instead of failing minutes into a build.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = "io_rf2_gmt_release"
ADDON_PAGE = ("https://forum.studio-397.com/index.php?threads/"
              "blender-rfactor2-export-scripts.74821/")
MIN_FREE_GB = 10

# Ask Blender itself rather than guessing where its add-ons live (installed
# and portable Blenders keep them in different places). Enabling it also
# proves this Blender can load it: it ships compiled for Python 3.10, which
# Blender 4.1 and later no longer use.
PROBE = (
    "import sys, bpy, addon_utils\n"
    "print('AC2RF2 VERSION', bpy.app.version_string)\n"
    "m = addon_utils.enable(%r, default_set=False)\n"
    "v = m.bl_info.get('version') if m else None\n"
    "print('AC2RF2 ADDON', '.'.join(map(str, v)) if v else ('ok' if m else 'missing'))\n"
    "sys.stdout.flush()\n" % ADDON
)


class Report:
    def __init__(self):
        self.problems = 0

    def ok(self, what):
        print("   ok       %s" % what)

    def note(self, what):
        print("   note     %s" % what)

    def problem(self, what, fix=None):
        self.problems += 1
        print("   PROBLEM  %s" % what)
        if fix:
            for line in fix.splitlines():
                print("            %s" % line)


def layouts(track_dir):
    return sorted(f[len("models_"):-len(".ini")] for f in os.listdir(track_dir)
                  if f.lower().startswith("models_") and f.lower().endswith(".ini"))


def check_track(rep, track_dir, layout):
    if os.path.isfile(track_dir):
        ext = os.path.splitext(track_dir)[1].lower()
        if ext in (".rar", ".7z"):
            rep.problem("%s is a %s archive" % (os.path.basename(track_dir), ext),
                        "Extract it first (7-Zip or WinRAR) and give the converter "
                        "the track folder. Only .zip files are extracted automatically.")
        else:
            rep.problem("%s is a file, not a track folder" % track_dir)
        return
    if not os.path.isdir(track_dir):
        rep.problem("track folder not found: %s" % track_dir)
        return
    name = os.path.basename(os.path.normpath(track_dir))
    if not any(f.lower().endswith(".kn5") for f in os.listdir(track_dir)):
        rep.problem("no .kn5 model files in %s" % track_dir,
                    "Give the track's own folder, e.g. "
                    "...\\assettocorsa\\content\\tracks\\ks_silverstone")
        return
    have = layouts(track_dir)
    if layout:
        if layout not in have:
            rep.problem("track %s has no layout called %r" % (name, layout),
                        "Its layouts: %s" % (", ".join(have) or "none (leave --layout out)"))
            return
        rep.ok("track %s, layout %s" % (name, layout))
        return
    # Without a layout the converter reads models.ini, and the AI line from
    # the track's own ai\ folder. A track whose layouts all live in
    # models_<name>.ini has neither, and would come out as its main model only.
    default = (os.path.exists(os.path.join(track_dir, "models.ini"))
               or os.path.isdir(os.path.join(track_dir, "ai")))
    if have and not default:
        rep.problem("track %s has no default layout: choose one" % name,
                    "Its layouts: %s\n(--layout <name>, or type it when convert.bat asks)"
                    % ", ".join(have))
        return
    rep.ok("track %s" % name)


def check_blender(rep, blender):
    if not os.path.isfile(blender):
        rep.problem("Blender not found at %s" % blender,
                    "Install Blender 3.6 LTS, or pass --blender \"<path to blender.exe>\"")
        return
    try:
        r = subprocess.run([blender, "--background", "--factory-startup",
                            "--python-expr", PROBE],
                           capture_output=True, text=True, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        rep.problem("Blender at %s did not start: %s" % (blender, exc))
        return
    found = {}
    for line in out.splitlines():
        if line.startswith("AC2RF2 "):
            key, _, value = line[len("AC2RF2 "):].partition(" ")
            found[key] = value.strip()
    version = found.get("VERSION")
    if not version:
        rep.problem("Blender at %s did not run the check" % blender,
                    "Its output ended with:\n" + "\n".join(out.splitlines()[-5:]))
        return
    rep.ok("Blender %s" % version)
    if not version.startswith("3.6"):
        rep.note("the converter is tested with Blender 3.6 LTS only; "
                 "the rF2 add-on supports 3.3 to 3.6")
    addon = found.get("ADDON", "missing")
    if addon == "missing":
        rep.problem("the rF2 add-on (%s) is not installed in this Blender" % ADDON,
                    "Download it from the Studio-397 forum, thread \"Blender rFactor2 "
                    "Export Scripts\":\n%s\nThen in Blender: Edit > Preferences > "
                    "Add-ons > Install..., and pick the downloaded zip." % ADDON_PAGE)
    else:
        rep.ok("rF2 add-on %s" % addon)


def check(track_dir=None, layout=None, blender=None, install=None,
          need_blender=True):
    """Print one line per check; True when nothing is missing."""
    import steam_paths
    rep = Report()
    print("checking setup")

    v = sys.version_info
    if v < (3, 10):
        rep.problem("Python %d.%d is too old" % v[:2],
                    "Install Python 3.10 or newer from python.org")
    else:
        rep.ok("Python %d.%d.%d" % v[:3])
    try:
        import numpy
        rep.ok("NumPy %s" % numpy.__version__)
    except ImportError:
        rep.problem("NumPy is not installed for this Python (%s)" % sys.executable,
                    "Run:  python -m pip install numpy")

    surfaces = os.path.join(steam_paths.AC_DIR, "system", "data", "surfaces.ini")
    if os.path.exists(surfaces):
        rep.ok("Assetto Corsa  %s" % steam_paths.AC_DIR)
    else:
        rep.problem("Assetto Corsa not found (no %s)" % surfaces,
                    "If it is installed somewhere Steam does not list, pass "
                    "--ac \"<assettocorsa folder>\"")

    if not os.path.isdir(steam_paths.RF2_DIR):
        rep.problem("rFactor 2 not found (looked for %s)" % steam_paths.RF2_DIR,
                    "If it is installed somewhere Steam does not list, pass "
                    "--rf2 \"<rFactor 2 folder>\"")
    else:
        rep.ok("rFactor 2      %s" % steam_paths.RF2_DIR)
        tdf = os.path.join(steam_paths.LOCATIONS, "terrain.tdf")
        for path, what in ((steam_paths.TEXCONV, "texconv.exe"),
                           (tdf, "ModDev\\Locations\\terrain.tdf")):
            if not os.path.exists(path):
                rep.problem("%s is missing from rFactor 2 (%s)" % (what, path),
                            "In Steam: rFactor 2 > Properties > Installed Files > "
                            "Verify integrity of game files")

    if install:
        if os.path.isdir(install):
            rep.ok("install into   %s" % install)
        else:
            rep.problem("install folder not found: %s" % install)

    if track_dir:
        check_track(rep, os.path.abspath(track_dir), layout)

    if need_blender:
        check_blender(rep, blender)

    free = shutil.disk_usage(HERE).free / 1e9
    if free < MIN_FREE_GB:
        rep.note("only %.0f GB free on this drive; a build writes several GB "
                 "into out\\" % free)

    if rep.problems:
        print("\n%d problem(s) found: fix them and run again." % rep.problems)
        return False
    return True


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("track_dir", nargs="?")
    ap.add_argument("--layout")
    ap.add_argument("--blender")
    ap.add_argument("--rf2")
    ap.add_argument("--ac")
    args = ap.parse_args()
    if args.rf2:
        os.environ["AC2RF2_RF2_DIR"] = os.path.abspath(args.rf2)
    if args.ac:
        os.environ["AC2RF2_AC_DIR"] = os.path.abspath(args.ac)
    if not args.blender:
        from build_track import BLENDER
        args.blender = BLENDER
    import track_zip
    if args.track_dir and track_zip.is_zip(args.track_dir):
        args.track_dir = track_zip.extract(args.track_dir)
    import steam_paths
    ok = check(args.track_dir, args.layout, args.blender, steam_paths.LOCATIONS)
    if ok:
        print("\nall good")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
