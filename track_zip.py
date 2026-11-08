"""Use an Assetto Corsa track straight from its download zip.

    python track_zip.py "<track>.zip"      prints the extracted track folder

The track is extracted into out\\_zips\\<zip name>\\, not into Assetto Corsa,
and reused while the zip is unchanged. A track folder is the shallowest folder
in the zip that holds .kn5 models: CSP extras keep more of them deeper down,
in extension\\, and a zip may wrap the track in a version folder. Messages go
to stderr, so convert.bat can read the folder from stdout alone.
"""
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ZIPS = os.path.join(HERE, "out", "_zips")


def say(text):
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def track_folders(names):
    """The shallowest folders holding .kn5 files, as zip paths ('' is the root)."""
    found = set()
    for name in names:
        parts = name.split("/")
        if "__MACOSX" in parts or parts[-1].startswith("._"):
            continue                    # macOS metadata, not models
        if parts[-1].lower().endswith(".kn5"):
            found.add("/".join(parts[:-1]))
    if not found:
        return []
    depth = min(f.count("/") + (1 if f else 0) for f in found)
    return sorted(f for f in found if f.count("/") + (1 if f else 0) == depth)


def extract(zip_path):
    """Extract the zip's one track and return its folder."""
    zip_path = os.path.abspath(zip_path)
    stem = os.path.splitext(os.path.basename(zip_path))[0]
    try:
        z = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise SystemExit("cannot open %s: %s" % (zip_path, exc))
    with z:
        names = [i.filename for i in z.infolist()]
        folders = track_folders(names)
        if not folders:
            raise SystemExit("%s holds no Assetto Corsa track (no .kn5 models)"
                             % os.path.basename(zip_path))
        if len(folders) > 1:
            raise SystemExit("%s holds %d tracks (%s). Extract it and give the "
                             "converter the one you want."
                             % (os.path.basename(zip_path), len(folders),
                                ", ".join(f.split("/")[-1] for f in folders)))
        dest = os.path.join(ZIPS, stem)
        track = os.path.normpath(os.path.join(dest, *folders[0].split("/"))) \
            if folders[0] else dest
        stamp = "%d %d" % (os.path.getsize(zip_path), int(os.path.getmtime(zip_path)))
        marker = os.path.join(dest, ".from_zip")
        try:
            same = open(marker).read() == stamp and os.path.isdir(track)
        except OSError:
            same = False
        if same:
            say("using %s, already extracted" % os.path.basename(zip_path))
            return track
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        say("extracting %s ..." % os.path.basename(zip_path))
        # extractall drops absolute paths and "..", so nothing lands outside dest.
        z.extractall(dest)
    with open(marker, "w") as f:
        f.write(stamp)
    return track


def is_zip(path):
    return path.lower().endswith(".zip") and os.path.isfile(path)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__.splitlines()[2].strip())
    print(extract(sys.argv[1]))
