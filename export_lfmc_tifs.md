# export_lfmc_tifs.py

Companion doc for `export_lfmc_tifs.py`.

## What it is

A small-scale proof that we can regenerate the Ai2 LFMC per-point image stacks
ourselves from Google Earth Engine, instead of pulling them from the private
`gs://presto-tifs` bucket. It processes the first few label points (5 by
default), and for each one builds the multi-band image and checks it against the
documented input contract (`docs/lfmc_input_spec.md`) before anything is
exported.

It is deliberately NOT the full export. It refuses any `--sample-size` over 50.
The ~90k-point run is a separate, deliberate job we only start once the contract
is confirmed and the open questions for Ana are answered.

## Why it proves the export is correct

Two design choices make the proof trustworthy:

1. **It reuses Galileo's own `create_ee_image`.** That is the exact function
   that produced the real tiles (in `lfmc-ai2/submodules/galileo`). We import it
   rather than re-implementing the sensor stacking, so the band set and order
   cannot drift from the contract. We only add the Galileo root to `sys.path`;
   we do not install or modify the upstream repo.

2. **It verifies band-for-band, server-side, before export.** `verify_contract`
   pulls the image's band names once and asserts:
   - total band count equals `T*18 + 17`, and the dynamic block is divisible by
     18 (the reshape the loader depends on),
   - the first 18 bands are `ALL_DYNAMIC_IN_TIME_BANDS` in order
     (`VV, VH, B2..B12, temperature_2m, total_precipitation_sum, def, soil, aet, avg_rad`),
   - the trailing 17 are the 16 `SPACE_BANDS` (SRTM, DW, WC) then the single
     LandScan static band.
   `T` is predicted independently from the `pad_dates` window with
   `expected_timesteps`, which mirrors Galileo's 30-day stepping loop, so a band
   count that does not match flags a window-drift bug instead of passing
   silently.

If any check fails the run stops with a clear AssertionError naming the
mismatch.

### Verified against live GEE

A 5-point dry-run against the live API passed 5/5: every point returned
`total=251` bands (`T=13`, `13*18 + 17`), with the first 18 and trailing 17 in
the right order.

One real finding from that run, now baked into the check: the WorldCereal bands
export with their **bare** names (`temporarycrops..irrigation`), not `WC_*`,
because `worldcereal.py` renames to the bare product name while
`dynamic_world.py` adds a `DW_` prefix. The Ai2 loader does not care (it reads by
position and count, never by TIF band name), so this is correct and matches the
real tiles. The verification compares against the names GEE actually emits, not
the loader's internal `WC_*` names. See `docs/lfmc_input_spec.md`, the "Band
naming caveat" note.

## What each point becomes

```
[ T * 18 dynamic bands ]  ++  [ 16 space bands ]  ++  [ 1 static band ]
```

- Dynamic per timestep (18): S1 `VV,VH` + S2 `B2..B12` + ERA5 `temperature_2m,total_precipitation_sum` + TerraClimate `def,soil,aet` + VIIRS `avg_rad`.
- Space (16): SRTM `elevation,slope` + Dynamic World x9 + WorldCereal x5.
- Static (1): LandScan `b1`.
- NDVI, location `x,y,z`, and the DW/WC static averages are NOT exported. They
  are computed later, during the TIF-to-array step in the Ai2 loader.
- Footprint 1000 m box, 10 m scale (~100x100 px), CRS `EPSG:4326`, named
  `<sorting_id>.tif`.

See `docs/lfmc_input_spec.md` for the full contract with file/line citations.

## How the temporal window is set

Per point, `pad_dates(label_date)` (copied from `lfmc/core/padding.py`) returns
a one-year window ending ~1 month after the label date. Galileo steps 30 days at
a time across it, yielding ~13 raw timesteps; the Ai2 loader later tail-slices
to the most recent 12. This is the single biggest reconstruction assumption and
is flagged as open question Q1 for Ana in the spec.

## Usage

```bash
# from the nasa-acres-maui repo root:
pip install earthengine-api pandas
earthengine authenticate            # one-time

# dry-run: build + verify the first 5 points, queue nothing (default)
python export_lfmc_tifs.py

# same, but actually queue the Drive exports
python export_lfmc_tifs.py --export

# verify a few more points, still no export
python export_lfmc_tifs.py --sample-size 10
```

| Flag            | Default | Meaning |
|-----------------|---------|---------|
| `--sample-size` | 5       | Points to process. Capped at 50 (this is the proof, not the full run). |
| `--export`      | off     | Queue Drive exports. Default is verify-only. |
| `--labels-csv`  | the lfmc-ai2 CONUS CSV | Path to `lfmc_data_conus.csv`. |

Exit code is non-zero if any point fails verification.

## Configuration

Edit the config block at the top:

- `EE_PROJECT` - GEE project id (`ace-shine-392702`, same as the GVMI baseline).
- `LABELS_CSV` - path to `lfmc_data_conus.csv` in the lfmc-ai2 clone.
- `GALILEO_ROOT` - the `submodules/galileo` dir in the lfmc-ai2 clone. It carries
  a `galileo` symlink to `src`, so this dir on `sys.path` makes `galileo.data...`
  importable (the same trick the smoke test uses).
- `DRIVE_FOLDER` - Drive output folder (created on first export). We export to
  Drive, never the private bucket.

## Key decisions

- **Reuse, do not reimplement, the band stacking.** The strongest proof that our
  tiles match is to build them with the same code that built the originals.
- **Default to dry-run.** Verifying the contract costs one cheap server call per
  point and queues nothing. You opt into exports explicitly with `--export`.
- **Hard cap at 50 points.** A scaffold that could accidentally fan out to 90k
  exports is a footgun. The full run gets its own script and its own decision.
- **Copy the two label helpers locally.** `pad_dates` and the grouped
  `read_labels` are tiny and copied with citations, so this script needs only
  the Galileo path, not the whole `lfmc` package, importable.
- **Pin `EPSG:4326`.** The upstream batch export leaves CRS unset (open question
  Q2 for Ana). We pin a CRS so our tiles are well defined and consistent with
  the rest of the Maui pipeline.

## What it does not do

- Does not run the full ~90k export.
- Does not download the finished TIFs or convert them to h5py (that is the Ai2
  loader's job, via `--h5pys-only` once the tiles exist).
- Does not prove the S2 cloud composites are bit-identical to Ai2's; those
  depend on the exact scene set at run time (open question Q4 for Ana).
- Does not verify pixel values, only the band structure. Value-level checks come
  after the first real tiles land and can be compared against any sample tile
  Ana can share.
