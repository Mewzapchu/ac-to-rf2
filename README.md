# AC to rF2

Converts an **Assetto Corsa** track into an **rFactor 2** location. It brings
across geometry, materials, textures, surfaces and grip, the AI line, TV cameras,
lighting and shadows. It reads Custom Shaders Patch (CSP) settings when they are
present.

The converter is generic: it reads what each AC track declares rather than using
per-track hacks. It has been tested on Silverstone, Spa, Laguna Seca and Highlands
(drift layout).

> **Personal use only.** A converted track still contains the original track's
> models and textures. Do not redistribute converted tracks unless you have the
> rights to the source content.

---

## Requirements

| What | Version | Where to get it |
|---|---|---|
| Windows | 10 or 11 | The scripts call Windows tools (`texconv.exe`, Blender) |
| [Assetto Corsa](https://store.steampowered.com/app/244210/Assetto_Corsa/) | any | Steam. Provides the tracks to convert and `system/data/surfaces.ini` |
| [rFactor 2](https://store.steampowered.com/app/365960/rFactor_2/) | current Steam build | Steam. Includes ModDev, `texconv.exe` and the GMT exporter add-on |
| [Python](https://www.python.org/downloads/windows/) | 3.10 or newer | python.org. Tick **"Add python.exe to PATH"** in the installer |
| [NumPy](https://numpy.org/install/) | 1.24 or newer | `pip install numpy` |
| [Blender](https://download.blender.org/release/Blender3.6/) | **3.6 LTS** | blender.org release archive. The rF2 exporter add-on targets 3.6 |
| rF2 GMT exporter add-on (`io_rf2_gmt_release`) | ships with rF2 | `rFactor 2\Support\Tools\Plugins\io_rf2_gmt_release` |
| `texconv.exe` | ships with rF2 | `rFactor 2\Support\Tools\MapConverter\texconv.exe` |
| [Custom Shaders Patch](https://acstuff.club/patch/) | optional | acstuff.club. Used only for its per-track config (material tweaks, GrassFX) when installed |

### Default paths

The scripts assume the standard Steam and Blender locations:

```
C:\Program Files (x86)\Steam\steamapps\common\assettocorsa
C:\Program Files (x86)\Steam\steamapps\common\rFactor 2
C:\Program Files\Blender Foundation\Blender 3.6\blender.exe
```

If yours differ, pass `--blender "<path to blender.exe>"` to `build_track.py`.
You can also edit the constants at the top of `build_track.py` (`BLENDER`),
`bake_layers.py` and `fix_textures.py` (`TEXCONV`), and the `--ac-root` default
in `convert_track.py`.

---

## Setup

1. **Install Python and NumPy**
   ```
   pip install numpy
   ```
2. **Install Blender 3.6 LTS** to its default folder.
3. **Install the rF2 exporter add-on in Blender**
   1. Copy the folder
      `rFactor 2\Support\Tools\Plugins\io_rf2_gmt_release`
      into `%APPDATA%\Blender Foundation\Blender\3.6\scripts\addons\`.
   2. Start Blender, open *Edit > Preferences > Add-ons*, search for "rF2" and tick it.

   The converter turns the add-on on by itself on every run, so this is only
   needed once.
4. **Get the code**
   ```
   git clone https://github.com/Mewzapchu/ac-to-rf2.git
   ```

---

## Converting a track

### The easy way

Drag the AC track folder onto **`convert.bat`**. For example, drag
`...\assettocorsa\content\tracks\ks_silverstone`.

If the track has several layouts, the converter lists them and asks which one to
build. Press Enter for the default layout.

**How long it takes:** about **20–30 minutes** per track on average, ranging
from roughly **10 minutes** (e.g. Silverstone) to **over an hour** for large,
dense tracks like Spa. Blender does most of the work, and a fast CPU and SSD help
the most. `--skip-geometry` reruns only the later steps in a few minutes.

The result is:

- installed into `rFactor 2\ModDev\Locations\<track>`
- zipped as `out\<track>\<track>_rf2.zip`, ready to unzip into `ModDev\Locations` on another PC

### From the command line

```
python build_track.py "<AC track folder>" --install "C:\Program Files (x86)\Steam\steamapps\common\rFactor 2\ModDev\Locations" --zip
```

Useful options:

| Option | What it does |
|---|---|
| `--layout <name>` | Which layout of a multi-layout track: the part after `models_` in `models_<name>.ini` (e.g. `--layout gp`) |
| `--hour HH:MM` | Session start time written into the GDB (default `12:00`; e.g. `18:30` for a low evening sun) |
| `--zip` | Also write the ready-to-share zip |
| `--skip-geometry` | Reuse the meshes from the previous run and redo only the later steps (much faster) |
| `--blender <path>` | Use a Blender that is not in the default folder |

### Checking an install

```
python verify_install.py "<rF2>\ModDev\Locations\<track>" out\<track>
```

This checks that every texture the scene references exists, that draw ranges,
shadows and the track database are in place, and prints a summary.

### Driving it: Developer Mode (do this first)

The converter installs into `ModDev`, which only rF2's **Developer Mode** (Mod
Mode) loads. To start it:

- In Steam, press **Play** on rFactor 2 and choose the developer / Mod Mode
  option, or
- run `rFactor 2\Bin64\rFactor2 Mod Mode.exe`.

Pick the converted track; it appears under its AC folder name, e.g.
`ks_silverstone_gp`. It is the same game with the same physics. **Always test a
track here first:** if something is wrong, it is much quicker to fix and
reconvert before packaging.

### Optional: using it in normal rFactor 2

Normal rF2 (and online servers) only load tracks that are packed into `.mas`
archives inside an installed `.rfcmp` package.

**Why the converter doesn't do this:** rF2's `ModMgr.exe` has command-line
options for building MAS files and packages, but in the current build the MAS
creation option exits without writing anything, so the later steps can't run
either. The MAS format is encrypted and its files are signed, so writing one
from scratch isn't practical. That leaves rF2's own GUI tool, done by hand.

It takes about 5–10 minutes per track, in `rFactor 2\Support\Tools\MAS2.exe`:

1. **Create three MAS files.** Drag files from
   `ModDev\Locations\<track>\` into MAS2 and save each set as its own MAS:
   - `<track>_assets.mas`: everything in `Assets\GMTs` and `Assets\MAPs`, plus `<track>.tdf`
   - `<track>_materials.mas`: everything in `Assets\Materials`
   - `<track>_layout.mas`: everything in the inner `<track>\` folder (`.scn`, `.gdb`, `.aiw`, `.cam` and the rest)
2. **Package.** Click **Package** → **Create Single Cmp Package** →
   `<Add New Component>`. Name it after the track, set the type to
   **Location** and the version to `1.0`, then add the three MAS files and click
   **Package**. This writes `<track>.rfcmp`.
3. **Install.** Click **Install** (or open the `.rfcmp` with `Bin64\ModMgr.exe`).
   The track now shows up in normal rFactor 2.

See Studio 397's
[packaging tutorial](https://www.studio-397.com/wp-content/uploads/2016/12/rF2_Packaging_Tutorial.pdf)
for screenshots of these dialogs.

---

## Pipeline

`build_track.py` runs these steps in order:

| Step | Script | What it does |
|---|---|---|
| 1 | `convert_track.py` | Reads the kn5 models, materials, AC surfaces and CSP config, and writes `material_map.json` |
| 2 | `fix_textures.py` | Converts textures to formats rF2 loads |
| 3 | `bake_layers.py` | Turns AC multilayer materials into rF2 two-stage textures (base and detail), cut-outs and specular maps |
| 4 | `make_aiw.py` | Builds the AI line and pit lane from AC's `ai/` files |
| 5 | `make_cam.py` | Builds TV cameras from AC's camera sets |
| 6 | `blender_stage.py` | Builds the meshes and materials in Blender and exports `.gmt` through the rF2 add-on (with `backdrop.py`) |
| 7 | `finalize_track.py` | Writes the scene (`.scn`), track database (`.gdb`) and terrain (`.tdf`), then installs |

Other files:

- `kn5.py`: reader for AC's kn5 model format
- `probe_*.py`: diagnostic tools used while working out the rF2 formats
- `NOTES.md`: a running log of every finding, with the measurement behind each decision. Read this before changing how materials are translated.

---

## Known limitations

- **Near-car band:** on roads whose AC detail texture is very contrasty
  (Silverstone, Spa), a band of visible asphalt grain can show a few metres in
  front of the car. It is mitigated, not solved.
- **Grass:** CSP GrassFX has no rF2 equivalent. Grass brightness is approximated.
- **Weather and clouds** are set in the rF2 session, not by the converter.
- **Normal rF2** needs a manual packaging step (see *Optional: using it in normal rFactor 2*).

## Known bugs

- **Track Cameras** can be underground some(most)times, havent given it much importance.


<br>
<br>

---

<p align="center"><sub><i>All the code in this project was written by an LLM.</i></sub></p>
