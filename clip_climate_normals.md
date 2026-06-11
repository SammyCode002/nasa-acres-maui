# Climate covariate clip

**File:** `clip_climate_normals.py`
**Project:** NASA ACRES Maui (github.com/SammyCode002/nasa-acres-maui)
**Author:** Samuel Dameg (SammyCode002)

## What this builds

Clipped versions of the 11 statewide climate covariate grids, cut down to the Kula-to-Kihei corridor. The statewide grids are large; the corridor is tiny. Clipping once up front means every later step (stacking covariates, sampling at field sites, feeding the model) reads small local rasters instead of re-reading statewide data every time.

Input: `data/climate_normals/<variable>/` with 13 monthly ESRI grids each (Jan-Dec plus Annual).
Output: `data/climate_normals_clipped/<variable>/<grid_name>.tif`, same folder-per-variable layout.

The 11 variables: `soil_moisture`, `vpd`, `aet_mm`, `pet_penman_mm`, `land_cover`, `veg_cover_fraction`, `lai`, `veg_height`, `solar_radiation`, `rh` (relative humidity), `tair` (air temperature).

## How it works

1. Read the corridor polygon from `aoi/corridor.geojson` (the feature tagged `role: "aoi"`). The AOI is never hardcoded here.
2. For each variable, find the ESRI grids. An ESRI grid is a folder of `.adf` files, so the script looks for folders containing `hdr.adf` or `w001001.adf`, not a file extension.
3. Clip each grid to the AOI with `rasterio.mask(crop=True)` and write a GeoTIFF.
4. Print a summary table and exit non-zero if any variable is missing months.

## Key decisions (and why)

- **Clip only, never resample or reproject.** `mask(crop=True)` keeps the source pixel grid and transform, so 250 m stays 250 m and WGS84 stays WGS84. We change the container (ESRI grid to GeoTIFF), not the pixels. The script logs each output's pixel size so you can confirm 250 m held.
- **If a grid is in a different CRS, we move the AOI, not the raster.** Reprojecting the raster would resample it. Instead we reproject the small AOI polygon into the grid's CRS and clip there, leaving every pixel untouched. In practice the grids are already WGS84, so this is a safety net.
- **`all_touched=True`.** At 250 m, a strict "is the cell center inside" test can shave a row of cells off a small corridor. Keeping every touched cell preserves the fringe.
- **GeoTIFF in, LZW compressed.** Lossless, and climate grids compress well, so the clipped set stays small.
- **No CRS on a grid -> stamp WGS84.** Some ESRI grids ship without a `.prj`. Rather than emit an unreferenced GeoTIFF, the script stamps the documented WGS84 and warns.

## Debug logging (the 4x4 standard)

Every major step (`load_aoi`, `clip_grid`, `process_variable`) is wrapped by the same `debug_log` decorator used in `gvmi_baseline.py`, logging inputs, outputs, timing, and status on each call. If a run dies partway, the log already names the function, the grid, and how long it ran. `find_grids` and `_is_esri_grid` are small pure helpers and are left undecorated to keep the log readable.

## How to run

The system Python is the Windows Store stub, and Python 3.14 has no geospatial wheels yet, so pin 3.12 and let uv pull rasterio:

```bash
uv run --python 3.12 --with rasterio python clip_climate_normals.py
```

Or in a normal venv:

```bash
pip install rasterio
python clip_climate_normals.py
```

## Success criteria

- `data/climate_normals_clipped/` has one folder per variable
- monthly variables have 13 GeoTIFFs (Jan-Dec + Annual); land cover has 1 (it is static)
- every output stays at the source native 250 m (about 0.00225 deg at this latitude) and WGS84
- the run ends with `[DONE] all variables have their expected clipped grids` and exit code 0

## Notes / caveats

- Exit codes: 0 = every variable has its expected grids (131 total: 10 x 13 monthly plus 1 land cover), 1 = source folder missing, 2 = finished with gaps (a CHECK row in the summary).
- The atlas archives extract with a wrapper folder (for example `soil_moisture/SoilMoisture_month_raster/...`) and a shared ESRI `info/` workspace. `find_grids` searches recursively, so the wrapper is fine and the `info/` folder is correctly skipped (it is not a grid).
- Land cover is a single static grid, not a monthly series, so its folder holds 1 GeoTIFF (`landcover.tif`).
- Grid folder names carry the month, so they become the output filenames (for example `sl_mst_jan.tif`). Rename later if you want friendlier names.
- This reads the same AOI file as `gvmi_baseline.py`, so the climate covariates and the Sentinel-2 baseline are guaranteed to share an extent.
