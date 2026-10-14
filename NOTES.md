# Open list, 2026-09-28

The material and lighting translation is now in rF2's own terms rather than
AC's, taken from Studio 397's Joesville: material colours at rF2's convention,
scene ambient 126, sun 220, fog 50 to 5000. The compensation machinery that
existed to fight the old translation -- a 1.9 gain, a ceiling that clipped the
brightest materials, an ambient of 237, fog pushed to 60000, a gamma exponent
-- is all gone.

## Judge this build against official rF2 content, not against CSP
The reference changed deliberately. Assetto Corsa is running CSP, whose
tonemapping we cannot reproduce by translating materials: in the same frame its
ground is brighter than ours and its sky darker, which is a response curve, not
a coefficient. Measured, AC's total light is around 440 units where rF2 native
gives about 300. Chasing the remainder through material values is what produced
two days of compensations.

## Known, measured, and not fixable by translation
- The hillside sits near 0.82 of where it was, and around half of AC's. The
  composed textures are within 9% of AC's own albedo (sbanc 0.97, splat_maps
  0.97, sand_trap 0.95, asphalt 0.91), so this is light, not texture. Scene
  ambient is the knob if it reads too dark next to official content.

## Next, and the most promising thing found so far
The material flag bit. Studio 397's foliage carries 0x68902003, ours carries
0x68900023: they set 0x00002000 and we set 0x00000020. The addon labels 0x20
Transparency, which is alpha blending, and labels 0x2000 "Car Body", which is
plainly wrong for a tree -- so 0x2000 is almost certainly the alpha test bit.

Everything else about our cut-outs now matches theirs: threshold correct and at
the right offset, bump shader that supports testing, blend source and
destination identical. The material is simply flagged to blend rather than to
test, which is why four builds of changing values changed nothing.

Set 0x00002000 for alpha tested materials, probably instead of 0x00000020.
Try it on a handful of meshes first and look at the flag word in the gmt before
spending an hour.

## Open
1. White tree trunks. Not the specular placeholder, not the normal map
   (bark_NM measures R 0.501 G 0.502 B 0.984, the same convention as every
   other one). Under the new scheme bark sits at 0.79 against asphalt's 0.95,
   so it is darker than it was relative to the road. Re-judge before digging.
2. Gravel and sand could take slightly more contrast. Second stage detail
   textures are used by nothing else, so a contrast boost there is texture
   only and needs no re-export.
3. Trees drawing over nearer trees. Foliage is alpha tested now with AC's own
   ksAlphaRef, which should let it write depth and sort itself. Confirm.
4. Flickering. Eleven decal materials are lifted a centimetre along their
   vertex normals. Confirm, particularly off the asphalt where it started.
5. drift is several builds behind. Rebuild when convenient, it is minutes.

## Parked with reasons
- detailUVMultiplier: AC authors a detail scale on 37 materials; its value for
  sand_trap works out finer than ours, not coarser, so adopting it would not
  give the coarser gravel that was wanted. Needs a re-export.
- Vegetation saturation: trees and grass bind AC's texture directly, so it
  needs either texture processing or a per material colour.
- Retail packaging (.mas + .mft).

## Habit worth keeping
Two material changes went straight into hour long exports unverified and both
came back broken, the second as green boxes where the trees were. The native
conversion was checked on five materials first and the values landed exactly as
intended. Do that every time.

## The green panels: colour keying (the actual cause)

**The alpha reference does nothing unless colour keying is switched on.**
Texture flag `0x80` at offset +64 from the texture name is the enable. We had
been writing a correct threshold into a test nothing had turned on, so trees,
grass and fences drew as solid panels whatever the number was -- which is why
moving it from 26 to 77 to 191 changed nothing visible.

Found by diffing one of our fence gmts against `Fence_ChainLink.gmt`, which
does the same job with the same shader and works:

| offset | ours | official |
|--------|------|----------|
| +64 texture flags | `0x00000008` | `0x00200098` |
| +100 alpha reference | 191 | 191 |

Across both official tracks `0x80` is set on 18 texture entries, every one a
diffuse map and 17 of the 18 at a threshold of 191: their entire cut-out set.
Every opaque diffuse has reference 1 and the bit clear.

The property name matters, and only one of them works.
`probe_colorkey.py` sets each candidate on its own mesh and reads the
exported byte back:

| property | bit moves |
|----------|-----------|
| `Flag_0x00000080` | yes |
| `ColorKey` | yes |
| `Flag_0x00000080_Color_Key` | no |
| `Color_Key` | no |
| `Transparent_Alpha` | no |

The same trap as `Alpha_Reference` and the blend factors: the assignment is
accepted and silently ignored. `verify_install.py` now checks the bit, and
caught 229 of 229 cut-outs unkeyed in the build before this one.

Our textures were also missing `0x10` Linear_Filter, which all 359 official
texture entries carry and a fresh addon node leaves off.

## The alpha threshold (still right, but it was not the panel)

## The green panels, the flicker, the grass and the fences (threshold detail)

One cause, measured rather than guessed: **the alpha threshold, down the mip
chain.**

rF2 cuts out foliage with the alpha *test*, not blending. 73 of the 75
materials in Joesville and Loch Drummond carry blend factors One/Zero --
trees, 2D trees, shrubs, crowds, chain link and safety fences among them.
Only `Grassverges` and `RoadLines` really blend. Every cut-out they ship uses
an alpha reference of **191**.

Why 191 and not AC's figure. Building a mip averages the transparent
background into the leaves, so the *smaller* the mip, the more of a low
threshold's texture gets through:

| threshold | our trees mip0 -> mip8 | our grass | official Trees3D |
|-----------|------------------------|-----------|------------------|
| 26 (AC's) | 29% -> **46%**         | 33% -> **77%** | 50% -> **100%** |
| 191       | 25% -> 16%             | 30% -> 5%      | 45% -> 25%      |

At a low threshold the quad fills in with `[62 69 34]` dark green as it
recedes -- the green box with a tree in the middle -- and the mip the card
picks changing with distance is the flicker and the pop-in. Official content
at 26 would do the same, so it is the threshold and not our textures: our
tree atlas (29%/25%) behaves just like theirs (50%/45%).

Two things this also explains:

- Every gmt we shipped was *already* opaque One/Zero, because
  `mat["Blend_Source_List"] = 4` as a custom key is silently ignored, exactly
  like `Alpha_Reference` was. "Matching Joesville bit for bit did not cut
  anything" was true, and the reason was the number, not the blending.
- `wood_planks` got a threshold of 128 from the `ksAlphaRef or 0.5` fallback
  while its alpha is uniformly a fifth. The whole wooden fence was being
  discarded.

The threshold is now decided in `bake_layers.cutout_ref` from the texture
that actually ships, three ways:

- **hard mask** (<5% nothing in between) -> 191
- **gradient** (>20% in between) -> left exactly as it was. `gravel_brd` is
  28/28/44 -- the soft sand-to-gravel edge. Raising it would eat a third of
  the border and bring back the on/off line.
- **nothing transparent** -> no alpha test at all

## Reflectance: still open, and quantified

Official rF2 writes `ambient = diffuse = 1.0` on **all 75** materials, with
zero variation: the texture is the albedo and the scene light does the work.
We write AC's `ks` coefficients normalised against the track's upper quartile,
which puts the whole track at about 2.5x AC's reflectance.

Our textures are byte-identical to AC's (checked against the kn5: ratio 1.00
on bark, fences, metal grid and the tree atlas), so the texture pipeline is
not the problem. The white trunk is arithmetic:

| | effective albedo of a trunk |
|---|---|
| AC (`bark.dds` 130 lum x `ksDiffuse` 0.30) | **39** |
| official rF2 (`PalmTrunk` at diffuse 1.0)  | **61** |
| ours (130 x 0.75)                          | **98** |

Both references put a trunk at 39-61 and we are at 98.

Three translations were tried against the data and all three fail:

- *write AC's `ks` verbatim* -> the track goes to 0.4x and everything is dark
- *fold `ks` into the texture* -> works for hard surfaces (bark 39 vs their
  61, metal grid 42 vs 48) and collapses for foliage (trees 9.7 vs 55, grass
  18 vs 65), because AC's `ksTree` coefficients are not plain albedo and CSP
  boosts them further
- *clamp the effective albedo to official's ceiling* -> there is no ceiling
  to clamp to. Official albedo runs median 78, p75 115, max 255.

So AC's `ks` is not a reflectance in rF2's sense and no single factor
reconciles the two. What is left is to drop the global 2.5x lift, keep AC's
relationships without the 1.0 clamp (25% of materials currently pile up on
it), and put the overall level in the scene lighting -- one lever. That
rescales the road, sand, gravel and rubber, which are already signed off, so
it is a separate change and not one to slip in beside the alpha fix.

### Texture flag property names (measured, not guessed)

`probe_colorkey.py` sets each candidate on its own mesh and reads the byte
back out of the exported gmt. The addon is not consistent:

| bit | works | silently ignored |
|-----|-------|------------------|
| 0x80 colour key | `Flag_0x00000080`, `ColorKey` | `Flag_0x00000080_Color_Key`, `Color_Key`, `Transparent_Alpha` |
| 0x10 linear filter | `LinearFilter` | `Flag_0x00000010`, `Flag_0x00000010_Linear_Filter`, `Linear_Filter`, `Filter` |

Run the probe before trusting any new property. Three separate rounds of
this pipeline were lost to assignments that were accepted and dropped.

## The flicker: AC's LOD distances, which we were throwing away

Not the decals, and not a depth bias. AC states a distance range on nearly
every mesh and we ignored both numbers:

| mesh | lod_in | lod_out |
|------|--------|---------|
| `track_hi_1_SUB7`      | 0   | 500  |
| `track_low_014_SUB1`   | 450 | 1000 |
| `Jersey_Plastic_LOW_3` | 190 | 500  |
| `BBgrass.003`          | 0   | 200  |

In AC the high chunk draws out to 500 m and the low one from 450, so the two
coincide across a 50 m band. We drew every level at every distance, so
coplanar copies fought for pixels wherever they overlapped -- measured at the
two places the user named, `track_low_011_SUB1` against `track_hi_5_SUB0`,
same material, median height difference **0.0000 m**.

rF2 takes the same pair as `LODIn`/`LODOut` per instance, so they are now
carried straight across and every detail level is kept: 1045 of 1066 drawn
meshes have a range, nothing is deleted. 127 meshes used to be thrown away by
name matching, which also lost the far geometry that should appear in the
distance.

Three heuristics were tried and abandoned before the LOD data was found, and
each is worth not repeating:

- *keep the richer of two coplanar meshes* -- triangle count does not tell
  AC's levels apart at all. `track_low_011_SUB2` has 4982 triangles against
  `track_hi_5_SUB1`'s 5014, so "richer" retired the near copy and started
  deleting real road.
- *pair them one to one by footprint* -- AC chunks its low detail track
  differently from its high detail track, so one low sheet lies across parts
  of several high chunks and matches none of them.
- *cover the low sheet with the union of the high ones* -- they are only
  partial overlaps. A low sheet runs on past the high set onto ground no high
  mesh occupies, so dropping it leaves holes and keeping it flickers. No
  mesh-level rule can win that; only distance can.

The name-based pass and the coplanar pass both survive as fallbacks for the
21 meshes AC leaves unbounded.

Also worth knowing: AC's node matrices are row-major with the translation in
the **last row**, and a node inherits its parents', so the product runs child
first. Getting that backwards leaves every instanced prop at the origin,
where all 66 pit lane cones look perfectly coplanar with one another -- it
produced 338 "duplicate" pairs that did not exist.

## Reflectance: moved to rF2's own convention

All 75 official materials carry ambient and diffuse 1.0, with no variation.
We were writing AC's coefficients normalised against this track's upper
quartile, a 2.38x lift with the top 6 materials clamped at 1.0.

Measured against the albedo of 342 official textures, by share of drawn
triangles landing inside their p25-p75 band:

| | in band |
|---|---------|
| AC's ks x 2.38, clamped (what we shipped) | 71.3% |
| rF2 native 1.0/1.0                        | **85.2%** |

The materials it rescues are the ones that were reported wrong:

| material | effective albedo now | at 1.0 | official counterpart |
|----------|---------------------|--------|----------------------|
| `BBgrass`   | 42.6 | 71.5 | their grass 64.8 |
| `DB2X2_L01` canopies | 20.6 | 57.8 | their foliage 55 |
| `BBbushes`  | 25.5 | 71.5 | |

It costs the two materials whose AC texture is already brighter than rF2 ever
authors: `Trank_bark` and `concrete-barriers` go from 93 to 130. AC hides that
behind a low ksDiffuse and rF2 has nowhere to put it. The groove keeps AC's
own figure so the racing line stays dark.

Ruled out by measurement, do not retry: folding ks into the texture (works for
hard surfaces, collapses for foliage -- trees land at 9.7 against their 55),
and capping the effective albedo (there is no ceiling to cap to; official
albedo runs median 78, p75 115, max 255).

## Terrain: the wrong detail layer was being tiled

`splat-maps` is `ksMultilayer` with four detail layers blended by mask:

| layer | texture | AC weight |
|-------|---------|-----------|
| txDetailR | `Soil5.dds` | **multR 0.50** |
| txDetailB | `GrassDead0119...` | multB 0.15 |
| txDetailG | `Grass0117_1_L` | multG 0.07 |
| txDetailA | `terrain_asphalt` | multA 0.00 |

`single_detail` picked by mask coverage alone, which chose the dead grass on B
and dropped `Soil5` entirely -- dark dead grass tiled over the hills at 3.4 m.
AC's shader scales each layer by its own multX, so the pick is now coverage
times weight.

Note `laguna_seca_terrain_4k.dds` reads [43, 75, 115], blue dominant: it is
the base colour map of an aerial, not an albedo, and the mask is the separate
`laguna-seca-terrain-4k.dds`.

## Grass: what it actually is

`BBgrass` is **not** ground cover. Its face normals are entirely horizontal
(upward fraction 0.000): 316k triangles of upright billboards spread over
285,000 m2, with `BBbushes` another 40k on `ksTree`. Bounding boxes suggested
flat sheets and were wrong -- `upward_fraction` is the measure that holds.

So 191 is right for it and lowering the threshold would bring the green
panels straight back. What was wrong was the brightness (42.6 against their
64.8, fixed above) and that we drew it to the horizon when AC stops at 200 m,
which the LOD ranges now handle.

Still open if it does not look right: AC gives it one flat diffuse with no
normal map and no detail layer, where official grass is six textures on
`L2MULTIGRASS` (`Grass_Natural`, `Grass_Soil`, `Grass_Weeds`, a MULT and two
normals) with `L2SPECULARMAPT0XT1` for the verges. That is authored content we
cannot synthesise from one AC texture.

## The far mountains (not started, by the user's ordering)
One mesh: `Cylinder001`, material `horizont`, `horizont.dds` (2048x4096, lum
137.7), **192 tris**, radius 2812 m, always drawn, built by `backdrop.py`.

### Where the scene file gets its per-instance facts

`finalize_track` builds the scene from `gmt/exported.json`, which the geometry
stage writes -- so a new per-instance field added in `convert_track` reaches
the map but not the scene, and the first build after adding `lod_in`/`lod_out`
wrote no LOD keys at all despite every row carrying them. There is already a
join for exactly this (`render` is taken from the mapping rather than the
export record, so changing what is drawn needs no re-export); new fields have
to be added to that join. They are also recorded in `exported.json` now, but
the join is what the scene actually reads.

Check it the same way every time: `grep -cE 'LODOut=\([1-9]' <venue>.scn`
should count the instances that carry a real range, as distinct from the
collision shells closed off with `LODOut=(0.01)`.

## Pop-in: carrying AC's LOD ranges traded one fault for another

Carrying `lod_in`/`lod_out` across stopped the flicker and introduced obvious
pop-in, because AC fades between levels and rF2 switches hard. AC's own
numbers are brutal without a fade: trunks culled at **15 m**, canopies at 30,
tyre walls at 100, marshals at 150, grass at 200. 239 primary meshes vanished
within 300 m.

Now: a mesh with `lod_in > 0` only ever appears beyond that distance, so it is
the far stand-in for something already drawn from zero. Those 269 are dropped
and everything else is given no range at all -- one set of geometry drawn at
every distance. No switch to pop, no second copy to fight over pixels. The
eleven green billboard panels go too, since they are the only thing their
material draws.

Grass stops disappearing at 200 m as a side effect, which was the "it only
loads super close" complaint.

## Aiming reflectance at rF2's albedo band

A flat 1.0 everywhere was right for textures that are already rF2-ish and
wrong where AC's texture is much brighter and AC hides it behind a low
ksDiffuse. Measured effective albedos against official's p25 51 / median 78 /
p75 115:

| material | at 1.0 | aimed | what it is |
|----------|--------|-------|------------|
| `chrome` | 255.0 | 70 | the corkscrew buildings |
| `distanz` | 187.4 | 70 | distance boards |
| `metal_rust` | 165.6 | 70 | |
| `gstand_main` | 159.6 | 70 | grandstand seating |
| `altoparlante` | 154.8 | 70 | loudspeakers, 22k tris |
| `Trank_bark` | 130.1 | 70 | trunks |
| `concrete-barriers` | 129.9 | 70 | |
| `sbanc` | 115.1 | 70 | |
| `BBgrass` | 71.1 | 65 | their grass is 64.8 |

`factor = clamp(target / measured_albedo, 0.2, 1.0)`, with the albedo measured
in `bake_layers` on the shipping texture and stored as `albedo_lum`. Targets:
70 for an ordinary surface, 65 for cut-out foliage.

**What is exempt, and why the obvious test was wrong.** Ground keeps rF2's
plain 1.0. `track_surface` alone is no good as the test -- it is a
proximity-to-racing-line flag, so it also exempts the concrete barriers and
the rusted metal, which are exactly what was too bright. Flatness alone is no
good either: a grandstand's seating tiers face up as squarely as the road, and
`gstand_main` slipped through at 160 that way.

The test that holds is **decal, or flat AND (two stage OR track surface)**.
A two stage first stage is a base its detail modulates, not the final albedo,
so it must not be rescaled; and the markings are all one of decal or track
surface. That leaves road, curbs, markings, sand, gravel and terrain untouched
-- the surfaces already signed off -- and catches every upright structure.

`chrome` also had `ksDiffuse` 0.00, which AC means as "no diffuse, reflection
only"; our `float(...) or default` turned the literal zero into the default
and then native wrote 1.0, so it shipped at an albedo of 255.

## Terrain: a blended detail, not one layer of four

The hills reading as mud was my own doing. `splat-maps` blends four layers by
mask and rF2's second stage takes one, so picking by coverage tiled dead grass
over everything and picking by AC's weight tiled bare soil instead. Neither is
the terrain, which is soil AND grass AND dead grass.

`blend_details` now averages the layers in the proportion AC's shader sums
them -- the mask's coverage of each channel times that channel's `multX` --
and writes one detail texture. Laguna's terrain comes out Soil5 49%,
GrassDead 39%, Grass 12%. Uniform where AC's varies across the map, which is
the part one stage cannot express, but it is the right average and it reads as
ground rather than as whichever layer won. Several other materials improve
the same way (gravel is now Soil2 43% / Soil3 37% / Soil1 20%).

Worth recording against the original complaint: the terrain was **not** dark.
Its composed albedo measures 94.5, above official's `Grass_Soil` at 88.6 and
their median of 78, while the grass was 71.1. The hills were brighter than the
grass; what was wrong was colour and character, not brightness. So the fix was
the blend plus taking the grass down to 65, not lifting the terrain.

## Rubber contrast: a regression of mine, measured against the backup

The road did not get darker. Measured against `~/ac2rf2_backup` (the
2026-09-28 install with the signed-off "darker rubber"):

| | road rendered | rubber rendered | contrast |
|---|---|---|---|
| backup | 111.6 | 11.2 `[9 10 14]` | 10.0 : 1 |
| after the native move | 154.5 | 28.0 `[24 26 35]` | 5.5 : 1 |

The road went up 38%; the **rubber went up 2.5x**, diffuse 0.130 to 0.325,
which halved the contrast. `groove.dds` is itself blue (`[72 79 107]`); near
black that does not show, at 28 it does. The groove's exception was rewritten
wrong during the native move -- it was meant to keep AC's raw coefficient and
was given the old divide-by-upper-quartile lift. Back to raw: with today's
brighter road that is about 14:1, more than the version that was liked.

Lesson: when a user reports a surface "got darker", measure it against the
last version they approved before believing either of us. Here the perception
was real but the cause was the overlay, not the surface.

## Grass: the threshold stripped the blades (not upside down)

The user suspected it was upside down or cut in half. Rendering the atlas
over magenta at full alpha and at 191 settled it: the bushes survive 191
because their leaves are dense, but every thin grass tuft is stripped back to
the dense strip at its base -- "a rectangle of grass, not the middle part
higher". AC knew: `BBgrass` and `BBbushes` share `BBGRASS_D.dds` with 0.1 (26)
and 0.7 (178) respectively.

Orientation was checked and is fine. AC stores these V coordinates negative
(-0.999..-0.024) and relies on wrap addressing; our UV path is a linear flip
both ways with no clamp, and five cut-outs that already render correctly use
the same negative convention.

**The fix that makes AC's thresholds safe: coverage-preserving mips.**
`texconv -keepcoverage <ref>` rebuilds each mip so the share of texels passing
the threshold matches full size. Read back from the stored BC3 alpha blocks:

| | 2048 | 512 | 128 | 32 | 8 |
|---|---|---|---|---|---|
| AC's own mips @26 | 32.9% | 39.9% | 45.5% | 54.5% | **71.9%** |
| our copy @26 | 32.9% | 33.0% | 32.8% | 31.7% | 29.7% |

So 191 is retired as a workaround. Every hard cut-out now uses the threshold
AC authored it with, on its own copy (`<stem>_a<ref>.dds`) whose mips hold that
threshold -- one copy per texture and threshold, since coverage is preserved
for one at a time. Stored as `cutout_tex` in the map.

`bc3_alpha_mips` style decoding (header at 128, BC3 alpha block = two
endpoints then 16 three-bit indices) is the way to check this; simulating mips
by box-filtering the top level would not have shown whether texconv did it.

## Terrain: why "brighter than official" still read as mud

Two wrong turns before the right answer, both worth recording.

1. **multX is tiling, not weight.** I weighted the detail layers by
   coverage x multX and gave the soil 49%. `bake_material` already carries a
   comment recording this exact mistake being made and fixed before. Back to
   coverage alone.
2. **Neutral grain did not change the colour.** Our first stage already carries
   each layer's colour (the mask-painted low-frequency bake), so a coloured
   detail looked like a double application. Making the detail neutral grey did
   not move the composed average at all -- `[106 98 80]` either way -- because
   the per-channel compensation re-targets the base to the same colour. It is
   more correct and stays (off the racing surface only), but it was not the
   fix.

What was actually missing, measured against Joesville's terrain:

| | ours | official |
|---|---|---|
| grain contrast (std/mean) | 0.123 | 0.15 - 0.23 |
| normal map relief | flat, 0 deg | GRASS_NORM mean 5.1 deg |

Averaging four layer textures pixel by pixel cancels their grain against each
other (dead grass alone is 0.273). And with no normal map the hillside lit as
one smooth sheet. Now: grain contrast restored to the coverage-weighted
contrast of AC's own layers (0.262 on Laguna), and a tangent-space normal map
derived from that grain as a height field, wrap-around gradients so it tiles,
scaled until its mean tilt is Joesville's 5.1 degrees. Carried as
`two_stage[...]["detail_nm"]` and fed to `sBump` on UV_D.

Only off-track painted surfaces get this (the terrain and `sbanc`). The curbs,
gravel and asphalt variants are painted too but were signed off as they are.

## Round of 2026-10-05: what I broke, and the corrected approach

The previous round changed too many things in one build and two of them broke
work that had been signed off. Recording the mistakes as plainly as the fixes.

### Comparisons that ignored the scene light

I told the user the road had got *brighter* (material 111.6 -> 154.5). It had
got darker on screen, because the scene light fell at the same time:

| | ambient | sun | fog |
|---|---|---|---|
| backup (signed off) | 237 | 245 | starts 2 km |
| now | 126 | 220 | starts 50 m |

Lit horizontal surface, road on screen: backup 157.3, last build 143.0.
**Always compare on-screen values (albedo x scene light), never material
values alone, and never across builds with different scene files.**

### Rubber contrast is capped by opacity, not diffuse

`groove.dds` is 22% opaque on average, 46% at most, and rF2 blends it. So the
rubber darkens the road by 1.25x on average however dark its diffuse -- equal
in the backup and now. Lowering diffuse from 0.325 to 0.130 changed nothing
visible, which I had predicted would quadruple the contrast. Now the rubber
has its own copy at twice the opacity and grey (the texture is blue), and the
road under it (found by letting each rubber sample vote for the surface
beneath it: only `asphalt_norm`) is lifted 1.10 to restore exactly the 157:

| | typical | heaviest |
|---|---|---|
| before | 1.26 | 1.53 |
| now | **1.64** | **3.22** |

### Cut-outs: neither threshold works alone

- At 191 (rF2's convention) AC's textures lose their soft content: every fence
  panel's chain-link mesh disappears outright (fences keeps 49% of what AC
  draws), privacy screens go to speckle, grass blades strip to their base.
  This had been true since the 191 build and nobody noticed because the green
  panels were the bigger problem.
- At AC's own thresholds the canopies and bushes went see-through. Most
  likely because the same build turned on linear filtering (0x10) for the
  cut-outs for the first time: bilinear alpha erodes every leaf toward its
  core at a high threshold like 178. Every earlier, point-sampled build drew
  them solid.

The approach that satisfies both: keep the test at **191**, **remap alpha** so
that whatever AC draws at its own threshold lands at 191 and above, rebuild
mips **holding coverage at 191**, and **point sample** cut-outs. Checked at
native resolution, texel for texel, our copy at 191 against AC's original at
its own threshold: grass 99.97% identical, fences 99.85%, canopies 99.93%.
Stored mip coverage holds flat (grass 32.9 -> ~32 where AC's own mips fill in
to 72%).

Two measurement traps hit while checking this:
- resampling a texture to 512 px to "compare shapes" filters its alpha and
  invents a mismatch; compare at native size;
- whole-texture coverage is meaningless when two materials share an atlas.

### Walls: measure where the triangles sample, not the whole atlas

The reflectance target used the mean of the whole texture. A wall that uses
one bright corner of a large atlas averages dark, so it was never corrected:
`bridge-coopertires` averages 34 over its atlas and 89 where it is drawn.
`convert_track` now records up to 1500 triangle-centre UVs per material
(`uv_samples`, wrapped into the texture) and `bake_layers` measures the albedo
there. Caught: pit garages, containers/tents, the Red Bull and media
buildings, bridges, gazebo, marshal posts -- all 83 to 104 on screen, now 70.

### Process

One fix per symptom, each verified on the actual output (decoded mips,
native-resolution renders, on-screen values with the scene light) before the
hour-long build. When the user reports something got worse, measure it
against the last version they approved -- including the scene file.

## Agenda for the next session (read-only findings, nothing changed)

### 1. Rubber: put it back exactly as it was
The user wants the rubber as before and only the road brighter. Revert the
`RUBBER_OPACITY` grey copy (`rubber_tex`) -- groove back to `groove.dds` /
`tyres_add2.dds` at AC's raw coefficient -- and keep `ROAD_LIFT` on
`asphalt_norm`. The doubled opacity read as "artificially dark".

### 2. THE MISSING LAYER: CSP, which the user runs in AC
`assettocorsa/extension/config/tracks/loaded/ks_laguna_seca.ini` (807 lines,
v5, 2020) changes what AC shows, and the converter has never read it.

**`[GRASS_FX] GRASS_MATERIALS = splat-maps`** -- CSP draws procedural 3D grass
over the whole terrain at runtime (`MASK_MAIN_THRESHOLD = 0.1`,
`SHAPE_SIZE = 5`). That is the grassy ground in AC. It is not in the kn5, so in
rF2 the terrain shows bare with only the sparse `BBgrass` billboards on it:
"bright fresh grass growing on mud" is literally that. No texture or
reflectance fix on the terrain could have reproduced it, which is why every
attempt left it looking the same.

**Summer `MATERIAL_ADJUSTMENT`s** (`CONDITION = SEASON_SUMMER`) override the
kn5 coefficients:

| material | kn5 kd / ka | CSP summer kd / ka |
|---|---|---|
| `splat?` (terrain) | 0.60 / 0.70 | **0.72 / 0.40** |
| `BBgrass?` | 0.25 / 0.30 | **0.20 / 0.20** |
| `DB2X2?, Trank?` | 0.15-0.30 | **0.10 / 0.15** |
| `tree?, bush?, bark?, BBbush?` | | 0.20 / 0.20 |
| `gravel` | 0.35 / 0.40 | 0.42 / 0.35 |
| `asphalt?` | 0.38 / 0.42 | 0.42 / 0.35 |
| `sbanc` | 0.55 / 0.70 | 0.80 / 0.45 |

So in the user's AC the terrain has ~3.6x the grass's diffuse; in our build
they are near parity (1.0 vs 0.91). CSP darkens all vegetation and lifts the
terrain.

Also: `[LIGHTING] SPECULAR_MULT = 0.15` (track specular cut to 15%),
`[BOUNCED_LIGHT]` on buildings and vehicles, `Material_Horizon` with
`LocalContrast = 1.5, FogMultiplier = 2` (relevant to the far mountains),
and `TRACK_MAP` naming walls (`concrete-barriers, tyres`) and buildings.

Options to decide with the user:
- **read CSP's track config generically** in `convert_track` and apply the
  active `MATERIAL_ADJUSTMENT`s (summer) to the kn5 props before conversion --
  generic across every AC track with a CSP config, and it is the AC the user
  actually compares against;
- **emulate GrassFX**: rF2 has no procedural grass, so either generate grass
  billboard geometry over the terrain where the mask/threshold says grass
  (faithful, heavy), or bake a greener, grassier look into the terrain base
  where GrassFX would cover it (cheap, flat);
- at minimum, take CSP's grass/terrain balance so the billboards stop
  standing out against the ground.

### 3. Walls (pit garages especially), and white trucks
`Pitbox` (64k tris) is already corrected (diffuse 0.66, drawn albedo 105.6 ->
70) and still reads far too bright. The target brings the material's
*average* to 70, but a garage texture mixes dark doors and interiors with
white walls, so the white parts stay well above it. Vertical walls facing the
sun also get N.L ~0.87 against the ground's 0.5. Look at a high percentile of
the drawn texels rather than the mean for upright surfaces, and compare
against AC's own (CSP-adjusted) relative balance. `Vehicles1` (white trucks,
103.5 -> 70) is the same case and lower priority.

### Do not repeat
- compare on-screen values with the scene light, against the last approved
  build, scene file included;
- one change per symptom, verified on the output before the hour-long build.

## 2026-10-05 (2): CSP read generically; walls by their bright part; rubber back

- **Rubber**: as AC ships it again (no copy). The 2x opacity grey copy raised
  the measured contrast but read as artificially dark. Only `ROAD_LIFT` on
  `asphalt_norm` remains, which is what was asked for.
- **CSP**: `convert_track.csp_adjustments` reads
  `extension/config/tracks/loaded/<track>.ini` (and the per-track
  `extension/ext_config.ini`), takes every `MATERIAL_ADJUSTMENT` that is
  unconditional or `SEASON_SUMMER`, matches CSP's `?` wildcard, and stores the
  values as `props["csp_ksDiffuse"]` / `["csp_ksAmbient"]`. The exporter
  applies the ratio to our factor **only where CSP darkens and only off the
  ground**: grass x0.80 / ambient x0.67, canopies x0.67 / x0.50, trunks
  x0.33 / x0.50, `tree_set_1` x0.91 / x0.50, bushes and grandstand ambient.
  Where CSP brightens (terrain, asphalt, gravel, grandstand diffuse) it is
  left out for now.
- **Terrain, not changed, and why**: CSP gives it kd 0.72 (vs 0.60) but ka
  0.40 (vs 0.70). Under our scene light (ambient 126, sun 110 on flat ground)
  that nets out about 14% *darker*, so CSP does not explain the bright rocks in
  AC. The big terrain difference is CSP's `GRASS_FX`, still open.
- **Walls**: upright opaque surfaces now aim the brightest tenth of their
  drawn texels (`albedo_p90`) at 70, not the average. Pitbox whites 104 -> 70
  (average 47), white trucks 163 -> 70 (average 30, may overshoot),
  `Bld_pitgarage` 104 -> 70. Cut-outs keep the average target plus CSP.

## 2026-10-10: relative to the road, not to the kn5; the horizon as a cut-out

### "Some grass is right, some isn't"
`BBgrass` and `BBbushes` sample the same atlas and shipped at diffuse 0.589
and 0.925. CSP gives both 0.20, so in AC they match. The rule "apply CSP only
where it darkens against the kn5" skipped the bushes, because 0.20 is above
the kn5's 0.15. Comparing CSP against the kn5 is the wrong frame.

**The right frame is relative to the road** (the signed-off surface, found as
the one the rubber lies on). AC lights by texture x coefficient, so a
material's factor is its coefficient over the road's, times the road's lift:
grass and bushes 0.524 / 0.629, canopies and trunks 0.262 / 0.471, tree
cut-outs 0.524 / 0.629. Applied to vegetation CSP configures (cut-outs, or
coefficient below the road's), off the ground.

### Terrain: ~40% too dark since the native move
Relative to the road, AC lights the terrain far harder: CSP 0.72 / 0.40
against the road's 0.42 / 0.35 (kn5: 0.60 / 0.70 against 0.38 / 0.42). Under
our light (ambient 126, sun 110 on flat ground) that is 1.41x the road per
unit albedo. Native 1.0 gave parity and the old scheme clamped to the same
result, so the hills have never had it -- the "mud", the dull rocks. Diffuse
cannot exceed 1.0, so the terrain base is lifted 1.41 x the road's 1.10 =
**1.55**: mean [107 99 80] -> [165 153 124], dried dirt; rocks (brightest
1%) [229 208 205]; 0.01% of texels clip. Off-track ground with CSP figures.

My earlier claim that CSP nets the terrain 14% *darker* compared CSP with the
kn5. Against the road it is the opposite.

### Horizon: the white band above the ridge
`horizont.dds` is four panorama strips; each strip's sky is stored as pale
blue-white [176 208 238] with alpha 0. The backdrop shipped on `L2DIFFUSET0`,
blend 2/1, no colour key, and `backdrop.py` cut the sky out by deleting
transparent triangles on a coarse 4x4-block grid -- written before the alpha
test flag was known, as its own docstring says. Triangles straddling the ridge
stayed whole and drew the stored sky colour: the white band.

Now the backdrop is a cut-out like the trees (bump shader, colour key, 191 on
a remapped copy `horizont_a128.dds` with coverage-preserving mips, point
sampled). It stays opaque, so it writes depth and sorts behind everything --
the old "mountains in front of everything" came from *blending* it. The
geometric clip still runs and only removes fully clear triangles.

Checked before building: the cylinder maps texture-top to cylinder-top (each
of four segments shows the upper half of its strip), and at the up-facing
edges the texture is clean -- crisp alpha, mountain colour directly under the
sky, a 1 px pale fringe at most. The pale band visible in a render of the
strip lies in the half the cylinder never displays.

## 2026-10: first tracks beyond Laguna (Spa, Silverstone GP)

What a second and third track exposed, all generic:

- **Blender crashed on Spa** at mesh 1151/1728 (`EXCEPTION_ACCESS_VIOLATION` in
  `UI_popup_menu_end`). The exporter add-on reports a mesh's geometry issues
  with `WindowManager.popup_menu`; in `--background` there is no window and
  Blender dies. `blender_stage.silence_popups()` replaces it and prints the
  popup's text instead. Spa needs re-running.
- **Multi-layout tracks**: models in `models_<layout>.ini`, data/ai in
  `<track>/<layout>/`. `convert_track.layout_files()` is the one place that
  knows this; `--layout` runs through build_track, convert_track, make_aiw
  and make_cam, and the output is named `<track>_<layout>`. Repeated model
  entries load once.
- **Not yet supported**: `POSITION=` / `ROTATION=` on a model in models*.ini.
  Reported as a WARNING; the objects would sit at the origin. Highlands'
  drift layout has one (`100.kn5`), so it waits on this.
- **Untextured materials / logic markers**: Silverstone's `AC_AUDIO_*` carry
  a bare 3ds Max material with no texture, and exporting them wrote `sTex0`
  as a filename -- the scene-load fault. Meshes that are neither drawn nor
  collidable are no longer exported at all (587 on Silverstone, mostly the
  far LODs already dropped for pop-in), and any untextured material gets
  `_white.dds`. `verify_install` caught this before the track was loaded.
- `--zip` writes `out/<track>/<track>_rf2.zip`, unzips straight into
  `ModDev/Locations`. `convert.bat` is drag-and-drop and asks for a layout.
- Spa's rubber vote picked three surfaces (asph, carpet, kerb_new), so the
  road lift also touches its kerbs and carpet. Silverstone picked one.

## 2026-10-10: the normals bug, and one brightness frame for every track

User report on Silverstone: glass bright blue and opaque, stand ceilings and
their posts far too bright, every white thing blown out, grass far too bright,
ground grass a bit too bright, road texture repeat more visible than in AC.

### Every upright surface was lit as if it faced the sky (root cause)
`blender_stage.build_mesh` set the mesh property `Normals = 2`. The addon's
`process_polygon` (rF2_GMT_Export.pyc, disassembled with Python 3.10's `dis`)
picks the vertex normal by that property:

| Normals | written | measured (probe_normals.py, wall facing AC +X / floor) |
|---|---|---|
| '0' | `loop.normal` (split normals, custom ones included) | -1,0,0 ok / 0,1,0 ok |
| '1' | polygon face normal, Y/Z not swapped | -1,0,0 ok / 0,0,1 WRONG |
| else | constant (0,0,1) -> straight up | 0,1,0 WRONG / 0,1,0 |

So every wall, ceiling, post and truck side on every track we have built was
lit like flat ground. The old note "Normals=1 points the wrong way, 2 matches"
was measured on ground faces only, where "up" happens to be right. All the
albedo-ceiling work on walls, seats, white trucks and tree trunks was
compensating for this. Now 0.

### AC's authored normals were thrown away
Blender rebuilt normals from the winding. Measured with the kn5 normals
(scratchpad normals_cmp.py): Silverstone's 1.38M grass billboard triangles
carry normals straight up, 90 deg from their faces on 100% of them; crowds
the same; trees > 60 deg off on 48%. `kn5.py` now reads normals,
`split_node` carries them, `build_mesh` sets them as custom split normals,
and mode 0 writes them (probe: a wall with AC "up" normals comes out 0,1,0).

### One brightness frame, anchored on Laguna's road
Per-track road anchoring hid that CSP draws Silverstone's asphalt at
0.15/0.25 vs Laguna's 0.42/0.35: on screen 36 vs 54, so Silverstone's road
(and all vegetation tied to it) came out ~1.9x too bright vs AC. Now every
material: fd = kd / 0.42 * ROAD_LIFT, fa = ka / 0.35 * ROAD_LIFT (CSP values
where present), divided by whatever lift the bake already put in the texture
(road: ROAD_LIFT; lifted terrain writes 1.0 as signed off). Upright opaque and
non-vegetation cut-outs: min(frame, old albedo ceiling) -- never brighter than
before, darker where AC says (glass 0.05/0.10 -> 0.14/0.29 instead of 0.62).
Vegetation (ksTree/ksGrass, or CSP-configured cut-outs): the frame, no
ceiling. Groove: AC's raw value as signed off. On Laguna the anchor is its own
road, so road, terrain and vegetation are unchanged by the frame.

Caveat to check in the sim: CSP's per-track "grasstree fix" values for
ksTree/ksGrass can be extreme (Silverstone trees 0.05/0.03, Spa bbgrass
kd 0.0). Laguna's 0.20/0.20 is the only one signed off.

### CSP config missing at conversion time
CSP downloads a track's config into extension/config/tracks/loaded the first
time the track is loaded in AC. Silverstone had never been loaded with CSP
when it was converted, so it got no CSP at all (trees written at 1.0). Now a
WARNING, repeated at the end of the build.

### CSP model replacement (not needed for Silverstone)
Silverstone's config hides 5 glass objects and inserts silverstoneGlass.kn5:
same geometry, same values (opaque, kd 0.05 ka 0.10), renamed so CSP can
light the windows at night. Nothing to gain by converting it.

### Spa
Five backdrops failed to export: the old geometric sky cut multiplied them
50-250x past 65536 vertices. It is redundant when the backdrop is a cut-out
(colour key trims per pixel) and is now skipped then. Two of the five were
not backdrops at all but the racing line rubber (groove1/groove2, radius
~1350 m because they follow the lap): backdrop now excludes decals.

### models.ini POSITION/ROTATION
Applied (kn5.placement_matrix). ROTATION[0] is a heading about Y: Highlands
drift's start gantry (100.kn5, -17) lands 3.3 deg off the road heading under
it with this sign, 37 deg with the other. Pitch/roll have no stock example:
warned, not applied.

## 2026-10-10 (later): repeat, grass, shadows, ranges, seasons

User report after Silverstone/Spa/Highlands in sim: asphalt pattern visibly
repeats when driving straight (least at Highlands); grass too bright on every
track; Spa grass shows as squares filled with dark blue; Spa pops far away and
the rubber vanishes for a few seconds then returns; Highlands' big rocks far
too bright (AC darker, bluish).

**Road repeat -- AC's multX are UV tiling, not layer strength.** The second
stage tiled every detail every 2 m. AC samples a detail at meshUV x multR..A
(ksMultilayer) or x detailUVMultiplier (NMDetail). Evidence: CSP's
materials_pbr.ini (DetailNMMult "UV multiplier"; parallax variant picked by
whether MultR..A are equal, meaningful only for UV scales; `_multGR` ratio),
and values like 150 / 4200 on Spa buildings. Measured AC repeat vs ours:

| track | material | AC repeat | ours was |
|---|---|---|---|
| Silverstone | asphalt (multR 1.0) | 43.7 x 132.5 m | 2 x 2 m |
| Spa | asph (multR 0.8) | 12.9 x 95.4 m | 2 x 2 m |
| Laguna | asphalt_norm (0.8) | 214 x 15 m | 2 x 2 m |
| Highlands | Road (detailUV 14) | 0.8 x 6.1 m | 2 x 2 m |

Highlands is the one AC already repeats finely, which is why it looked right.
`bake_layers.ac_detail_tiling` now gives uv_b = AC's multiplier (highest
non-zero among layers carrying the chosen texture; all zero -> 0.001, i.e.
flat as in AC). Grains blended here (no AC figure) keep the 2 m rule.
`strength()` in the bake still reads multX as a weight; the Laguna terrain
blend was signed off on that and is left alone.

**CSP conditions are blends, not switches.** Spa's config is seasonal:
SEASON_SUMMER is a LUT over YEAR_PROGRESS that peaks at 0.7 and sits ~0.5 all
summer; CSP sets OFF + (VALUE-OFF) x weight. We took VALUE outright, so Spa's
bbgrass got ksDiffuse 0.0 / ksAmbient 0.07 (lit by blue sky alone) where AC
shows ~0.10 / 0.16. Now every SEASON_* condition with a YEAR_PROGRESS LUT is
evaluated at RACE_YEAR_PROGRESS 0.5 (July 1). Side effects: Silverstone trees
0.05/0.03 -> 0.144/0.18; Highlands `costone` (the big rocks) now gets its
SEASON_MUD figures 0.185/0.195 instead of the kn5's 0.23/0.25.

**Spa grass squares.** bbgrass is ksPerPixelAT, ref 0.5, but its alpha has a
soft ramp so `cutout_ref` called it a gradient and drew the card whole. The
gradient rule exists for ground blends (Laguna gravel_brd); upright (upward <
0.5) gradients now get the hard cut AC gives them.

**No shadow casters at all.** Our .scn never set ShadowCaster; Loch Drummond
sets it on 385 instances, (Static, Solid) or (Static, Texture) for cut-outs.
So no roof shaded its seating, no tree its grass, no rock itself. Now from
AC's per-mesh cast_shadows flag (`finalize_track.shadow_caster`), skipping
hidden, blended, backdrop, decal and flat (upward >= 0.9) meshes.

**Pop-in / vanishing rubber: the exporter's default range.** The add-on
writes LOD_Out = 1000.0 into every gmt (rF2_Utilities.set_gmt_defaults /
InitCustomProperties) and we never set it. rF2 culls by distance to the mesh
centre; Spa's rubber (Loft286/Object697) is 1.35 km in radius, so it dropped
out whenever its centre was >1 km away. build_mesh now sets LOD_Out = 13000
(the far clip) + the mesh's radius on the empty and the mesh.

**Grass darker, by intent.** User asked for it even if artificial. Rationale:
CSP GrassFX covers the [GRASS_FX] GRASS_MATERIALS ground with shaded blades in
AC; rF2 has nothing like it. Grass cards (vegetation with AC up-normals) x0.50
(GRASS_CARD_DIM); GrassFX ground x0.40 (GRASS_GROUND_DIM) only where the
surface's mean colour (base x detail mean, for two stage) is green-dominant
(sand/gravel are on those lists too). First pass used 0.70/0.85 and measured
the two-stage base alone, which is near white, so the ground was never dimmed
and the season blend left Silverstone grass at 0.91/0.43 against the 0.47/0.24
the user had already called too bright. The constants are anchored to that
feedback (each track's grass darker than the build judged), not measured --
tune them if needed.

### Correction (2026-10-11): AC's multiplier stretched every detail

Applying AC's multX as one UV factor for both axes stretched every road,
sand and grass detail along the track by the mesh's UV anisotropy (measured
per-triangle: Silverstone asphalt 43.6 x 130.9 m per UV, 3.0:1; Spa asph and
Highlands Road 7.2-7.6:1; Highlands grass_near 26:1). The user saw it at once.
Whatever AC does, it does not show the grain smeared, so the second stage is
square again. `repeat_metres`: AC's repeat on the shorter axis, never finer
than the old 2 m and never coarser than 128 detail texels per metre (8 m for
a 1024, 16 m for a 2048); uv clamp back to the old [1, 512]. Result: asphalt
at Silverstone/Spa/Laguna repeats every 8 m (was 2), Highlands road stays 2 m,
buildings stay 2 m.

## 2026-10-11: specular mask, road detail one mip down, darker grass

- AC masks specular by txDiffuse alpha where there is no txMaps; rF2 got a
  flat spec map at AC's full figure. Silverstone grass ksSpecular 1.0 with
  alpha mean 0.23 (sand-bordini 0.26, Spa sand 0.30): ~4x AC's shine, read as
  "fluorescent". `spec_mask` (bake) scales the written specular; roads barely
  move (their figure was clamped at 1 anyway).
- Road repeat visible only near the car, ending at a sharp line (user): the
  detail's mip 0 band (1024 texels per 2 m = 2 mm, 20-60x finer than AC shows
  it). Track-surface details now ship at half size (`*_half.dds`), so the
  near band shows what was drawn beyond the line. Hypothesis under test.
- Grass dims 0.50/0.40 -> 0.35/0.30 ("still too fluorescent").
- Built with --hour 17:30 (GDB session starts). Clouds are an in-game
  weather setting, not a track file.

### The near-car band: the detail NORMAL map's tiling (2026-10-11)

Screenshots (Silverstone, 7): coloured glitter (blue/white/orange specks) on
the asphalt for ~10-15 m in front of the car, smooth beyond and smooth in the
mirrors. Compared with Highlands, where the user saw it least:

| track | road normal map | specular |
|---|---|---|
| Silverstone asphalt | txDetailNM tarmac-detail5_NM2, tiled with the detail at 2 m | 2.2 x mask 0.41 |
| Spa asph | same texture, same tiling | 3.0 x 0.47 -> 1.0 |
| Laguna asphalt_norm | tarmac_detail_NM_B, same tiling | 0.02 x 4 (tiny) |
| Highlands Road | flat_NM.dds (txNormal) | 4.0, nothing to catch |

AC's txDetailNM is on the mesh UVs (CSP's Kunos-multilayer replica: "mapped
with UV1" x DetailNMMult, default 1), not the detail's 2 m tiling: bumps 20-60x
too dense, glittering under specular until the mips flatten them. Now AC's
txDetailNM keeps the base mapping; only bake-derived grain normals tile with
their detail. The half-size road detail (HALVE_ROAD_DETAIL) is switched off --
it was the wrong hypothesis. Grass cards 0.35 -> 0.25.

### Same day, next test: caustics, and grass that ignores material figures

- txDetailNM on the mesh UVs: metre-wide blobs reading as frozen water
  caustics (user screenshot 20261011230622). Neither mapping matches AC, so
  AC's txDetailNM is now dropped on two stage surfaces (txNormal or flat, as
  Highlands' road always had). Bake-derived grain normals still tile.
- Grass cards: installed KSBB_HI001_KSLAYER5.gmt carries amb 0.146 / dif
  0.068 (was 0.41/0.24) and the user saw no change. rF2 lights mostly from its
  sky (IBL) onto the texture; its own content keeps material figures at 1.0.
  Lever moved to the texture: grass cards' cut-out copies are written with
  RGB x GRASS_CARD_TEXTURE (0.5), `*_a<ref>_d50.dds`. Grass card = cut-out
  vegetation under 1 m tall with up normals or an AT/ksGrass shader
  (`is_grass_card`; pines and hedges have up normals too, hence the height).

### The near band, measured: detail contrast (2026-10-12)

Flat normal map tried in sim: glare gone, band still there -> reverted to the
2 m tiled txDetailNM. Contrast (luminance std/mean) per mip, box-filtered:

| detail | m0 | m2 | m4 |
|---|---|---|---|
| Silverstone tarmac-detail5 (1024^2) | 0.52 | 0.36 | 0.16 |
| Spa tarmac-detail6 (1024^2) | 0.53 | 0.32 | 0.13 |
| Laguna tarmac_detail_B (1024^2) | 0.31 | 0.21 | 0.08 |
| Highlands Tarmac_5D_A (1024x4096) | 0.34 | 0.27 | 0.11 |

The band is the near region where the detail is still at full contrast;
Highlands' is 4x denser along the road, so its band sits near the wheels
(user's observation). Driving-surface details above DETAIL_MAX_CONTRAST 0.34
are written as `*_soft.dds`, scaled toward their per-channel mean (mean
unchanged, so the base pre-scale holds). Grass card texture 0.5 -> 0.75.
- Final choice (user: "as much like AC as possible"): asphalt under the rubber
  gets AC's exact tiling (uv_b = multR on both axes, as AC applies it); sand
  and grass keep 2 m. SOFTEN_DETAIL off (it had flattened the terrain grass).

## 2026-10-12: AC's exact asphalt tiling reverted; detail softened instead
Silverstone asphalt has multR 1, so AC's tiling spread tarmac-detail5 (and its
normal map) over 44 x 132 m: huge stretched stones with the sky glinting off
them. Back to the signed-off 2 m. The near-car band is handled by softening
the detail's contrast to 0.34 (Highlands/Laguna level), now applied only to
the asphalt under the rubber, so the terrain grass is untouched.
Softening alone left the band exactly as visible (user, same day): it scales
every mip equally, so the drop between mips is unchanged. Decoding the stored
mips showed it: tarmac-detail5 is 0.52 0.55 0.48 0.21 0.05 (std/mean, mip 0-4),
a cliff at mip 2->3. Now the asphalt detail is rewritten with an even-contrast
chain (even_mip_detail: 0.28 through mip 3, then x0.75 per mip).
Grass cards x0.875. Silverstone at 19:00 (sunset) for testing.
Even mips made the band MORE visible (user): grain held further out is the
problem, not the cliff. Reverted (EVEN_MIPS = False). Now matching Highlands on
both measured quantities: contrast 0.34 (uniform softening) and density --
Highlands packs 4 copies of its detail per 2 m tile along the road, a 0.5 m
repeat, and its band sits under the wheels. Asphalt detail now every 0.5 m
(ROAD_DETAIL_METRES); its normal map stays at 2 m via two_stage uv_nm. 18:30.
Road had no sun glare: every material without txMaps got _flat_spec.dds,
which is BLACK, so the asphalt's specular was zeroed. The asphalt under the
rubber now gets AC's actual mask (txDiffuse alpha, mean 0.41) as its spec map
(road_spec_map -> two_stage["spec"]), and spec_mask is not applied on top.
Grass cards x0.8.
