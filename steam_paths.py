"""Where Steam put Assetto Corsa and rFactor 2 on this machine.

Steam can install a game into any of its library folders, listed in
steamapps/libraryfolders.vdf. Hardcoding C:\\Program Files (x86)\\Steam broke
the converter the moment rFactor 2 was moved to another drive.
"""
import os
import re

STEAM_DEFAULT = r"C:\Program Files (x86)\Steam"


def libraries():
    """Every Steam library folder, the default one first."""
    out = [STEAM_DEFAULT]
    vdf = os.path.join(STEAM_DEFAULT, "steamapps", "libraryfolders.vdf")
    try:
        text = open(vdf, encoding="utf-8", errors="replace").read()
    except OSError:
        return out
    for path in re.findall(r'"path"\s+"([^"]+)"', text):
        path = path.replace("\\\\", "\\")
        if os.path.normcase(path) not in (os.path.normcase(p) for p in out):
            out.append(path)
    return out


def game_dir(folder):
    """steamapps/common/<folder> in whichever library has it, else the default."""
    for lib in libraries():
        p = os.path.join(lib, "steamapps", "common", folder)
        if os.path.isdir(p):
            return p
    return os.path.join(STEAM_DEFAULT, "steamapps", "common", folder)


RF2_DIR = game_dir("rFactor 2")
AC_DIR = game_dir("assettocorsa")
TEXCONV = os.path.join(RF2_DIR, "Support", "Tools", "MapConverter", "texconv.exe")
LOCATIONS = os.path.join(RF2_DIR, "ModDev", "Locations")


if __name__ == "__main__":
    import sys
    if "--locations" in sys.argv:       # for convert.bat
        print(LOCATIONS)
        sys.exit(0)
    print("rFactor 2:     %s" % RF2_DIR)
    print("Assetto Corsa: %s" % AC_DIR)
    print("texconv:       %s (%s)" % (TEXCONV, "found" if os.path.exists(TEXCONV) else "MISSING"))
    print("install into:  %s" % LOCATIONS)
