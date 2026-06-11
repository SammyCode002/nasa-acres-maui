"""
clip_climate_normals.py

NASA ACRES Maui - clip the statewide climate covariate grids to the corridor.

Takes the 8 "Climate of Hawai'i" / Evapotranspiration atlas variables (each a
folder of 13 monthly ESRI grids: Jan-Dec plus Annual) and clips every grid to
the Kula-to-Kihei corridor in one run. Output is GeoTIFF, same folder-per-
variable layout, native 250 m resolution and WGS84 preserved (this is a clip,
not a resample or reproject).

The corridor extent is read from aoi/corridor.geojson, the project's single
source of truth. There is no bounding box hardcoded in this file on purpose: if
the AOI changes, it changes in one place and this script follows.

Author : Samuel Dameg (SammyCode002)
Project: NASA ACRES Maui  (https://github.com/SammyCode002/nasa-acres-maui)

Run (the system Python is the Windows Store stub and Python 3.14 has no
geospatial wheels yet, so pin 3.12 and let uv pull rasterio):

    uv run --python 3.12 --with rasterio python clip_climate_normals.py
"""

import functools
import json
import logging
import sys
import time
from pathlib import Path

import rasterio
from rasterio.mask import mask
from rasterio.warp import transform_geom

# --------------------------------------------------------------------------- #
# Config  (the only block you should normally need to edit)
# --------------------------------------------------------------------------- #

# Paths are resolved relative to this file so the script runs from anywhere.
BASE_DIR = Path(__file__).resolve().parent
AOI_PATH = BASE_DIR / "aoi" / "corridor.geojson"
SRC_DIR = BASE_DIR / "data" / "climate_normals"
OUT_DIR = BASE_DIR / "data" / "climate_normals_clipped"

# The 8 covariates, one folder per variable under SRC_DIR.
VARIABLES = [
    "soil_moisture",
    "vpd",
    "aet_mm",
    "pet_penman_mm",
    "land_cover",
    "veg_cover_fraction",
    "lai",
    "veg_height",
    "solar_radiation",
    "rh",
    "tair",
]

# Monthly variables have 13 grids: Jan-Dec plus Annual.
EXPECTED_MONTHS = 13

# Land cover is a single static layer, not a monthly series, so it has 1 grid
# where the others have 13. A set keeps this easy to extend if more static
# layers (for example a DEM) are added later.
STATIC_VARIABLES = {"land_cover"}

# all_touched keeps every cell the AOI polygon touches, not just cells whose
# center falls inside. WHY: at 250 m a strict center test can shave a row of
# cells off the edge of a small corridor; we would rather keep the fringe.
ALL_TOUCHED = True

# WGS84 is what the AOI is in and what the grids are documented as. We use this
# as a fallback only if a grid ships without its own CRS defined.
FALLBACK_CRS = "EPSG:4326"

# --------------------------------------------------------------------------- #
# 4x4 debug logging  (inputs, outputs, timing, status) via a decorator
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("clip_climate_normals")


def _short(obj, limit=120):
    """Truncate a repr so big paths/arrays don't flood the log."""
    text = repr(obj)
    return text if len(text) <= limit else text[:limit] + "..."


def debug_log(func):
    """Log the 4 things we care about every call: inputs, outputs, timing, status.

    WHY a decorator: it wraps any function without us editing its body, so every
    step self-reports the same way. When a clip dies at grid 73 of 104, the log
    already shows which function failed, on what input, and after how long. No
    print-statement archaeology.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        logger.info(
            "[INPUT ] %s args=%s kwargs=%s",
            func.__name__,
            _short(args),
            _short(kwargs),
        )
        try:
            result = func(*args, **kwargs)
            elapsed = time.perf_counter() - start
            logger.info("[OUTPUT] %s -> %s", func.__name__, _short(result))
            logger.info("[TIME  ] %s took %.3fs", func.__name__, elapsed)
            logger.info("[STATUS] %s OK", func.__name__)
            return result
        except Exception as exc:  # noqa: BLE001  (we re-raise after logging)
            elapsed = time.perf_counter() - start
            logger.exception(
                "[STATUS] %s FAILED after %.3fs: %s",
                func.__name__,
                elapsed,
                exc,
            )
            raise

    return wrapper


# --------------------------------------------------------------------------- #
# Core steps
# --------------------------------------------------------------------------- #


@debug_log
def load_aoi(path):
    """Read the corridor geometry from the GeoJSON single source of truth.

    WHY select role == "aoi": corridor.geojson also holds the cross-validation
    block features. We want the one polygon marked as the AOI, not the blocks.
    If nothing is marked (an older/hand-edited file), we fall back to the first
    polygon so the script still does something sensible.

    Returns (geometry_dict, aoi_crs). The geometry is a GeoJSON-like dict, which
    is exactly what rasterio.mask wants.
    """
    if not path.exists():
        raise FileNotFoundError(f"AOI file not found: {path}")

    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    features = data.get("features", [])
    aoi = next(
        (f for f in features if f.get("properties", {}).get("role") == "aoi"),
        None,
    )
    if aoi is None:
        aoi = next(
            (f for f in features if f.get("geometry", {}).get("type") == "Polygon"),
            None,
        )
        if aoi is None:
            raise ValueError(f"No usable polygon found in {path}")
        logger.warning("No feature tagged role='aoi'; using first polygon instead")

    # GeoJSON is WGS84 by spec unless a CRS member says otherwise.
    return aoi["geometry"], FALLBACK_CRS


def _is_esri_grid(folder):
    """True if a folder looks like an ESRI grid (a folder of .adf files).

    WHY: an ESRI grid is a directory, not a single file. GDAL recognizes it by
    the header/data .adf files inside, so we check for those rather than a file
    extension.
    """
    if not folder.is_dir():
        return False
    names = {p.name.lower() for p in folder.iterdir()}
    return "hdr.adf" in names or "w001001.adf" in names


def find_grids(variable_dir):
    """List (label, grid_path) for every ESRI grid under one variable folder.

    Searches recursively, so it does not matter whether the grids sit directly
    in the variable folder or one level down inside the archive's wrapper folder
    (for example soil_moisture/SoilMoisture_month_raster/sl_mst_jan). The label
    is the grid's folder name, which encodes the month and becomes the output
    GeoTIFF's filename so the month survives the clip.
    """
    if not variable_dir.is_dir():
        return []
    grids = [
        (p.name, p)
        for p in sorted(variable_dir.rglob("*"))
        if p.is_dir() and _is_esri_grid(p)
    ]
    return grids


def expected_count(variable):
    """Grids a variable should have: 1 if it is a static layer, else 13."""
    return 1 if variable in STATIC_VARIABLES else EXPECTED_MONTHS


@debug_log
def clip_grid(grid_path, geom, aoi_crs, out_path):
    """Clip one ESRI grid to the AOI and write it out as a GeoTIFF.

    WHY no resample or reproject: we want the covariate exactly as published,
    just cut to the corridor. mask(crop=True) keeps the source pixel size and
    transform, so 250 m stays 250 m. We only convert the container format
    (ESRI grid -> GeoTIFF), not the pixels.

    WHY transform the geometry, not the raster: if a grid happens to be in a
    different CRS than the AOI, reprojecting the raster would resample it. So we
    instead reproject the small AOI polygon into the grid's CRS and clip there,
    leaving every pixel untouched.
    """
    with rasterio.open(grid_path) as src:
        src_crs = src.crs

        geom_for_mask = geom
        if src_crs is not None and src_crs.to_string() != aoi_crs:
            # Move the polygon into the raster's CRS so the clip lines up.
            geom_for_mask = transform_geom(aoi_crs, src_crs.to_string(), geom)

        clipped, out_transform = mask(
            src, [geom_for_mask], crop=True, all_touched=ALL_TOUCHED
        )

        profile = src.profile.copy()
        # Drop the source's tiling/block hints. ESRI grids carry block sizes
        # that GDAL rejects on a plain striped GeoTIFF (a harmless warning, but
        # it would repeat on every grid). We write a striped, LZW GeoTIFF.
        for key in ("blockxsize", "blockysize", "tiled"):
            profile.pop(key, None)
        profile.update(
            driver="GTiff",
            height=clipped.shape[1],
            width=clipped.shape[2],
            transform=out_transform,
            tiled=False,
            compress="lzw",  # lossless; climate grids compress well
        )
        # If the grid had no CRS, stamp the documented WGS84 so the output is
        # still georeferenced rather than floating in pixel space.
        if profile.get("crs") is None:
            profile["crs"] = FALLBACK_CRS
            logger.warning("%s had no CRS; stamping %s", grid_path.name, FALLBACK_CRS)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(clipped)

        # Pixel size straight from the transform, so we can confirm 250 m held.
        px_x, px_y = abs(out_transform.a), abs(out_transform.e)
        return {
            "out": out_path.name,
            "shape": (clipped.shape[1], clipped.shape[2]),
            "pixel_deg": (round(px_x, 6), round(px_y, 6)),
            "crs": str(profile.get("crs")),
        }


@debug_log
def process_variable(variable, geom, aoi_crs):
    """Clip every monthly grid for one variable. Returns a small summary dict."""
    variable_dir = SRC_DIR / variable
    out_dir = OUT_DIR / variable

    grids = find_grids(variable_dir)
    if not grids:
        logger.warning(
            "[%s] no ESRI grids found in %s (is the data extracted there?)",
            variable,
            variable_dir,
        )
        return {"variable": variable, "found": 0, "clipped": 0, "failed": 0}

    clipped, failed = 0, 0
    for label, grid_path in grids:
        out_path = out_dir / f"{label}.tif"
        try:
            clip_grid(grid_path, geom, aoi_crs, out_path)
            clipped += 1
        except Exception:  # noqa: BLE001  (already logged by the decorator)
            failed += 1

    expected = expected_count(variable)
    if len(grids) != expected:
        logger.warning(
            "[%s] found %d grids, expected %d",
            variable,
            len(grids),
            expected,
        )

    return {
        "variable": variable,
        "found": len(grids),
        "clipped": clipped,
        "failed": failed,
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def main():
    """Clip all 8 variables to the AOI in one run.

    Success criteria:
      - data/climate_normals_clipped/ has one folder per variable
      - monthly variables have 13 GeoTIFFs (Jan-Dec + Annual); land cover has 1
      - every output stays at the source 250 m and WGS84
    """
    logger.info("[START ] AOI=%s", AOI_PATH)
    logger.info("[START ] source=%s", SRC_DIR)
    logger.info("[START ] output=%s", OUT_DIR)

    if not SRC_DIR.is_dir():
        logger.error(
            "Source folder not found: %s\n"
            "Put the extracted ESRI grids under data/climate_normals/<variable>/ "
            "(one folder per variable, each with its 13 monthly grids), then re-run.",
            SRC_DIR,
        )
        sys.exit(1)

    geom, aoi_crs = load_aoi(AOI_PATH)

    summaries = [process_variable(v, geom, aoi_crs) for v in VARIABLES]

    # Plain-text summary table so the run ends with an at-a-glance status.
    logger.info("[SUMMARY] variable             found  clipped  failed")
    all_ok = True
    for s in summaries:
        expected = expected_count(s["variable"])
        flag = "OK" if (s["clipped"] == expected and s["failed"] == 0) else "CHECK"
        if flag == "CHECK":
            all_ok = False
        logger.info(
            "[SUMMARY] %-20s %5d  %7d  %6d  %s",
            s["variable"],
            s["found"],
            s["clipped"],
            s["failed"],
            flag,
        )

    total_clipped = sum(s["clipped"] for s in summaries)
    expected_total = sum(expected_count(v) for v in VARIABLES)
    logger.info(
        "[SUMMARY] %d / %d grids clipped into %s",
        total_clipped,
        expected_total,
        OUT_DIR,
    )

    if all_ok and total_clipped == expected_total:
        logger.info("[DONE  ] all variables have their expected clipped grids")
        sys.exit(0)
    else:
        logger.warning("[DONE  ] finished with gaps; see CHECK rows above")
        sys.exit(2)


if __name__ == "__main__":
    main()
