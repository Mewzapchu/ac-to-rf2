# Working on ac-to-rf2

Instructions for AI coding assistants, and for people, changing this converter.
Read `README.md` first for what it does and the pipeline. Read `NOTES.md`
before changing how materials, lighting or textures are translated: it records
every finding with the measurement behind it, including approaches that were
tried and reverted.

## Ground rules

- Find the cause and prove it with a measurement before changing anything (a
  value read from the kn5 or a texture, a log line, a timing). Don't tune
  numbers by rebuilding and looking. Add what you measured to `NOTES.md`.
- Keep fixes generic. The converter reads what each AC track declares
  (`models*.ini`, `surfaces.ini`, CSP config); don't add special cases for one
  track.
- Assetto Corsa with Custom Shaders Patch is the visual reference.
- Some mod tracks are deliberately scrambled (`ek_nagao`). Don't try to break
  that protection; see the README section on scrambled tracks.
- Never commit converted track content. `out/` is ignored by git for that
  reason.

## Running it

```
python check_setup.py
python build_track.py "<AC track folder>" [--layout <name>] [--hour HH:MM] [--install auto]
python verify_install.py "<rF2>\ModDev\Locations\<track>" out\<track>
```

- Without `--install` a build stays in `out\<track>`; with it, it also replaces
  the track in rF2's `ModDev\Locations`.
- With the default `--jobs` a build takes about 5-7 minutes on a 12-thread CPU
  (several Blenders side by side); `--jobs 1` runs one at a time, 11-15
  minutes. `--skip-geometry` reruns everything except the Blender stage.
- Each step is a separate process that reads the current files, so don't edit
  the code while a build runs from the same folder. Experiment in a git
  worktree instead.

## Comparing two builds

- Built from the same code and options, everything except `.gmt` files comes
  out byte identical: textures, `material_map.json`, `.scn`, `.gdb`, `.aiw`,
  `.cam`.
- `.gmt` files are not reproducible, not even between two single-Blender runs.
  The rF2 exporter leaves uninitialised values in the normals, tangents and
  padding of degenerate vertices, and rounds some tangents differently in the
  last bit. Positions and texture coordinates are identical. On Highlands the
  four `pub_lod2_*` meshes differ on every run. Compare `.gmt` files allowing
  for that, not byte for byte.
- `--jobs 1` rules the parallel build out when something looks different.

## Testing

- `verify_install.py` checks that every texture the scene references exists,
  and that draw ranges, shadows and the track database are in place.
- The real test is driving the track in rF2's Developer Mode (README, "Driving
  it").

## Files

- Keep each file's existing line endings. `convert.bat` must stay CRLF.
