"""Assemble an rFactor 2 ModDev location from the converted parts.

rF2 wants a venue folder holding the terrain file and an Assets tree, with the
scene, database, waypoint and camera files in a layout subfolder:

  Locations/<venue>/
      <venue>.tdf
      Assets/GMTs/*.gmt
      Assets/MAPs/*.dds
      <layout>/
          <layout>.scn  .gdb  .AIW  .cam

The scene file is rebuilt from the gmt files that were actually exported rather
than from the pre-export plan.
"""
import os
import json
import shutil
import argparse

SCN_HEADER = """CUBEASF
// {layout}, converted from Assetto Corsa

MaterialPath={venue}\\Assets\\Materials
SearchPath=.
SearchPath={venue}
SearchPath={venue}\\{layout}
SearchPath={venue}\\Assets\\GMTs
SearchPath={venue}\\Assets\\MAPs

MASFile=Commonmaps.MAS

View=mainview
{{
  Clear=False
  Color=(0, 0, 0)
  Size=(1.00, 1.00) Center=(0.5, 0.5)
  FOV=(77.75, 31.25)
  ClipPlanes=(0.50, 13000.00)
  View=rearview
  {{
    Clear=False
    Color=(0, 0, 0)
    Size=(0.200, 0.100) Center=(0.50, 0.01)
    FOV=(77.75, 31.25)
    ClipPlanes=(0.50, 500.00)
  }}
}}

GroupMethod=Dynamic
MaxShadowRange=(500.00)
AmbientColor=({amb}, {amb}, {amb})
ReflectPlane=(0.000, 1.000, 0.000, 0.000)
// Studio 397's own figures, from Joesville. We had these, then moved them to
// chase a CSP screenshot, which was the wrong reference.
FogMode=LINEAR FogIn=(50.00) FogOut=(5000.00) FogDensity=(0.00015) FogColor=(205, 215, 235)

// A scene with no light source at all is not something rF2 will render.
Light=Direct00
{{
 Type=Directional Color=({sun}, {sun}, {sun}) Dir=({sx:.3f}, {sy:.3f}, {sz:.3f})
}}

// Screen space reflections need both of these defined or loading warns and
// the background mappers fail.
ReflectionMapper=STATIC01
{{
  Type=Cubic
  TextureSize=(512)
  UpdateRate=(0.100)
  StaticSwitch=(300.000)
  Pos=({cx:.2f}, {cy:.2f}, {cz:.2f})
}}

ReflectionMapper=REFMAP0
{{
  Type=Cubic
  TextureSize=(1024)
  UpdateRate=(100.000)
  StaticSwitch=(300.000)
  TrackingIns=True
}}

"""

GDB = """{layout}
{{
  Filter Properties = StockV8 Historic Modern rFRS TMOD
  Attrition = 30
  Max Vehicles = 32
  TrackName = {layout}
  EventName = {layout}
  VenueName = {venue}
  Location = converted from Assetto Corsa
  Length = {km:.3f} KM / {mi:.2f} Miles
  TrackType = Road Course
  TerrainDataFile = ..\{venue}.tdf
  HeadlightsRequired = false
  FormationAndStart = 0
  GarageDepth = 3.5
  PitlaneBoundary = 1
  NumStartingLights = 2
  RaceLaps = 10

  // Sun high overhead. Change these to move the sun.
  TestDayStart = {hour}
  Practice1Start = {hour}
  Practice2Start = {hour}
  Practice3Start = {hour}
  QualifyStart = {hour}
  WarmupStart = {hour}
  RaceStart = {hour}

  Latitude = 40.0
  Longitude = 0.0
  Altitude = 0
}}
"""


# Where the light travels, from AC's own sun for the track. Falls back to the
# direction we used before when a track says nothing.
FALLBACK_SUN = (0.25, -0.95, 0.18)


def sun_direction(out_dir):
    """Turn AC's sun pitch and heading into an rF2 light direction.

    Pitch is degrees above the horizon, heading degrees around it. rF2 wants
    the direction the light travels, so everything points down and away. The
    X sign carries the AC to rF2 mirror; if a track ends up lit from the wrong
    side, that is the term to flip.
    """
    import math
    path = os.path.join(out_dir, "material_map.json")
    lit = {}
    if os.path.exists(path):
        lit = json.load(open(path)).get("lighting") or {}
    if "sun_pitch_angle" not in lit:
        return FALLBACK_SUN
    pitch = math.radians(float(lit["sun_pitch_angle"]))
    heading = math.radians(float(lit.get("sun_heading_angle", 0.0)))
    flat = math.cos(pitch)
    return (flat * math.sin(heading), -math.sin(pitch), -flat * math.cos(heading))


def shadow_caster(row, cutouts):
    """How this mesh casts a shadow in rF2, or None.

    Nothing did. The scene never named a caster, so roofs left the stand
    seating in full sun, trees left no shade on the grass and rocks none on
    themselves: a good part of every "too bright" report, where AC (and CSP,
    which turns casting on for more) shades them. Studio 397's Loch Drummond
    marks 385 instances (Static, Solid), or (Static, Texture) for cut-outs so
    the leaves cast and the cards around them do not.

    AC's own per-mesh flag decides. Left out: what is not drawn, blended
    surfaces and backdrops (rF2 casts the whole card), decals, and flat
    ground, which official tracks give a separate shadow object if anything
    and which would only shade itself.
    """
    if not row.get("render", True) or not row.get("cast_shadows"):
        return None
    if row.get("decal") or row.get("backdrop"):
        return None
    if row.get("needs_blend") and not row.get("alpha_test"):
        return None
    if float(row.get("upward", 0.0)) >= 0.9:
        return None
    if row.get("alpha_test"):
        return "Texture" if row["rf2_material"] in cutouts else None
    return "Solid"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", help="out/<track> from convert_track.py")
    ap.add_argument("--minimal", action="store_true",
                    help="diagnostic: only collidable meshes, no scenery")
    # Measured against AC the ground ran between 0.65 and 0.92 of its
    # brightness, so both go up. The sun goes up further than the ambient:
    # lifting the two together flattens a scene, and AC's is the crisper of
    # the two, not merely the brighter.
    ap.add_argument("--ambient", type=int, default=126,
                    help="scene ambient level, 0-255; lower if surfaces blow out")
    ap.add_argument("--sun", type=int, default=220,
                    help="directional light level, 0-255")
    ap.add_argument("--hour", default="12:00",
                    help="time of day for every session, e.g. 12:00")
    ap.add_argument("--install", default=None,
                    help="rF2 ModDev Locations folder to copy into")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out_dir)
    venue = os.path.basename(out_dir)
    layout = venue
    gmt_dir = os.path.join(out_dir, "gmt")
    exported = json.load(open(os.path.join(gmt_dir, "exported.json")))

    # Take the draw flags from the mapping rather than the export record, so
    # changing what is drawn does not mean re-exporting every mesh.
    mapping = os.path.join(out_dir, "material_map.json")
    cutouts = {}
    if os.path.exists(mapping):
        mdata = json.load(open(mapping))
        cutouts = mdata.get("alpha_ref", {})
        flags = {m["mesh"]: m for m in mdata["meshes"]}
        for row in exported:
            cur = flags.get(row.get("mesh"))
            if cur is not None:
                row["render"] = cur.get("render", True)
                row["shadow"] = shadow_caster(cur, cutouts)
                # AC's switching distances come from here too, for the same
                # reason: they decide what the scene says, not what the
                # geometry stage happened to record an hour earlier.
                row["lod_in"] = cur.get("lod_in", 0.0)
                row["lod_out"] = cur.get("lod_out", 0.0)

    stage = os.path.join(out_dir, "rf2", venue)
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    lay_dir = os.path.join(stage, layout)
    gmts = os.path.join(stage, "Assets", "GMTs")
    maps = os.path.join(stage, "Assets", "MAPs")
    for d in (lay_dir, gmts, maps, os.path.join(stage, "Assets", "Materials")):
        os.makedirs(d)

    # put the static reflection probe on the track, using the first waypoint
    cx = cy = cz = 0.0
    aiw_src = os.path.join(out_dir, venue + ".AIW")
    if os.path.exists(aiw_src):
        import re
        m = re.search(r"wp_pos=\(([-\d.]+),([-\d.]+),([-\d.]+)\)",
                      open(aiw_src, encoding="utf-8", errors="replace").read())
        if m:
            cx, cy, cz = (float(v) for v in m.groups())
            cy += 5.0

    lap_km = 0.0
    if os.path.exists(aiw_src):
        import re as _re
        m = _re.search(r"lap_length=([\d.]+)",
                       open(aiw_src, encoding="utf-8", errors="replace").read())
        if m:
            lap_km = float(m.group(1)) / 1000.0

    if args.minimal:
        keep = [r for r in exported if r["physical"]]
        print("MINIMAL build: %d of %d instances" % (len(keep), len(exported)))
        exported = keep

    # Opaque geometry first, then transparent, biggest first. Blended surfaces
    # do not write depth, so a huge backdrop drawn after the trees paints over
    # them.
    exported.sort(key=lambda r: (bool(r.get("blend")), -float(r.get("radius", 0.0))))

    sx, sy, sz = sun_direction(out_dir)
    lines = [SCN_HEADER.format(venue=venue, layout=layout, cx=cx, cy=cy, cz=cz,
                               amb=args.ambient, sun=args.sun,
                               sx=sx, sy=sy, sz=sz)]
    phys = 0
    for row in exported:
        name = os.path.splitext(row["gmt"])[0]
        coll = "True" if row["physical"] else "False"
        if row["physical"]:
            phys += 1
        # Timing strips collide but must not act as ground, or the car sits on
        # them instead of the road.
        hat = "False" if row.get("timing") else coll
        # AC marks some meshes as collision-only shells. Drawing them puts a
        # blank wall over the real road and barriers, so keep the collision but
        # close the visible range to nothing.
        invisible = row.get("timing") or not row.get("render", True)
        if invisible:
            extra = " LODIn=(0.0) LODOut=(0.01)"
        else:
            # AC's switching distances, carried straight across. Without them
            # every detail level of a surface drew at once and the coplanar
            # copies flickered against each other.
            lin = float(row.get("lod_in") or 0.0)
            lout = float(row.get("lod_out") or 0.0)
            extra = (" LODIn=(%.1f) LODOut=(%.1f)" % (lin, lout)) if lout > 0.0 else ""
        if row.get("shadow") and not invisible:
            extra += " ShadowCaster=(Static, %s)" % row["shadow"]
        lines.append("Instance=%s\n{\n  MeshFile=%s CollTarget=%s HATTarget=%s%s\n}\n"
                     % (name, row["gmt"], coll, hat, extra))
    write(os.path.join(lay_dir, layout + ".scn"), "\n".join(lines))
    write(os.path.join(lay_dir, layout + ".gdb"),
          GDB.format(venue=venue, layout=layout, km=lap_km,
                     mi=lap_km * 0.621371, hour=args.hour))

    tdf = os.path.join(out_dir, venue + ".tdf")
    if os.path.exists(tdf):
        shutil.copy2(tdf, os.path.join(stage, venue + ".tdf"))

    aiw = os.path.join(out_dir, venue + ".AIW")
    has_aiw = os.path.exists(aiw)
    if has_aiw:
        shutil.copy2(aiw, os.path.join(lay_dir, layout + ".AIW"))

    wanted = {r["gmt"].lower() for r in exported}
    n_gmt = 0
    for f in os.listdir(gmt_dir):
        if not f.lower().endswith(".gmt"):
            continue
        if args.minimal and f.lower() not in wanted:
            continue
        shutil.copy2(os.path.join(gmt_dir, f), gmts)
        n_gmt += 1

    n_tex = 0
    tex_dir = os.path.join(out_dir, "textures")
    if os.path.isdir(tex_dir):
        for f in os.listdir(tex_dir):
            src = os.path.join(tex_dir, f)
            if not os.path.isfile(src):
                continue
            shutil.copy2(src, maps)
            n_tex += 1

    # Without these three rF2 cannot build sectors or collision, and the track
    # crashes on load. Shipping one without them has happened twice.
    names = {os.path.splitext(r["gmt"])[0].lower() for r in exported}
    need = {"xfinish", "xsector1", "xsector2"}
    missing = sorted(need - names)
    if missing and not args.minimal:
        raise SystemExit("refusing to install: timing strips missing (%s). "
                         "Re-run the geometry stage." % ", ".join(missing))

    print("staged %s" % stage)
    print("  instances   %d  (%d collidable)" % (len(exported), phys))
    print("  gmt files   %d" % n_gmt)
    print("  textures    %d" % n_tex)
    print("  waypoints   %s" % ("yes" if has_aiw else "MISSING"))

    cam = os.path.join(out_dir, venue + ".cam")
    if os.path.exists(cam):
        shutil.copy2(cam, os.path.join(lay_dir, layout + ".cam"))
        print("  cameras     converted from AC")
    elif args.install:
        fallback = os.path.join(args.install, "default.cam")
        if os.path.exists(fallback):
            shutil.copy2(fallback, os.path.join(lay_dir, layout + ".cam"))
            print("  cameras     placeholder (default.cam)")

    if args.install:
        dest = os.path.join(args.install, venue)
        try:
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            shutil.copytree(stage, dest)
            print("installed -> %s" % dest)
        except PermissionError as exc:
            print("could not install (needs elevation): %s" % exc)
            print("copy this folder there by hand: %s" % stage)


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


if __name__ == "__main__":
    main()
