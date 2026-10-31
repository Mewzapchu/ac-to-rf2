"""Where Steam put Assetto Corsa and rFactor 2 on this machine.

Steam can install a game into any of its library folders, listed in
steamapps/libraryfolders.vdf. Hardcoding C:\\Program Files (x86)\\Steam broke
the converter the moment rFactor 2 was moved to another drive, and would miss
everything for anyone whose Steam itself lives elsewhere, so Steam's own folder
is read from the registry, where Steam records it.

Either game can also be named outright, for a copy Steam does not know about:
build_track.py --rf2 / --ac, which set AC2RF2_RF2_DIR / AC2RF2_AC_DIR for every
step it runs.
"""
import os
import re

STEAM_DEFAULT = r"C:\Program Files (x86)\Steam"


def steam_root():
    """Steam's own folder: from the registry, else the default install path."""
    try:
        import winreg
    except ImportError:
        return STEAM_DEFAULT
    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    for hive, key, value in keys:
        try:
            with winreg.OpenKey(hive, key) as k:
                path = winreg.QueryValueEx(k, value)[0]
        except OSError:
            continue
        if path and os.path.isdir(path):
            # Steam stores it lower case; realpath gives the folder's own case.
            return os.path.realpath(path)
    return STEAM_DEFAULT


def libraries():
    """Every Steam library folder, Steam's own first."""
    root = steam_root()
    out = [root]
    if os.path.normcase(STEAM_DEFAULT) != os.path.normcase(root):
        out.append(STEAM_DEFAULT)
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    try:
        text = open(vdf, encoding="utf-8", errors="replace").read()
    except OSError:
        return out
    for path in re.findall(r'"path"\s+"([^"]+)"', text):
        path = os.path.normpath(path.replace("\\\\", "\\"))
        if os.path.normcase(path) not in (os.path.normcase(p) for p in out):
            out.append(path)
    return out


def game_dir(folder):
    """steamapps/common/<folder> in whichever library has it, else Steam's own."""
    for lib in libraries():
        p = os.path.join(lib, "steamapps", "common", folder)
        if os.path.isdir(p):
            return p
    return os.path.join(steam_root(), "steamapps", "common", folder)


RF2_DIR = os.environ.get("AC2RF2_RF2_DIR") or game_dir("rFactor 2")
AC_DIR = os.environ.get("AC2RF2_AC_DIR") or game_dir("assettocorsa")
TEXCONV = os.path.join(RF2_DIR, "Support", "Tools", "MapConverter", "texconv.exe")
LOCATIONS = os.path.join(RF2_DIR, "ModDev", "Locations")


if __name__ == "__main__":
    import sys
    if "--locations" in sys.argv:       # for scripts that only need this
        print(LOCATIONS)
        sys.exit(0)
    print("Steam:         %s" % steam_root())
    print("rFactor 2:     %s" % RF2_DIR)
    print("Assetto Corsa: %s" % AC_DIR)
    print("texconv:       %s (%s)" % (TEXCONV, "found" if os.path.exists(TEXCONV) else "MISSING"))
    print("install into:  %s" % LOCATIONS)
