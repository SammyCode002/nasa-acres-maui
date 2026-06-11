"""
gvmi_baseline.py

NASA ACRES Maui - First baseline map generator.

Computes monthly vegetation-moisture index maps from Sentinel-2 over the
Kula-to-Kihei corridor (Aug 2023 to present) and exports each monthly
composite to Google Drive as a GeoTIFF.

Bands produced per month:
    GVMI  - Global Vegetation Moisture Index (primary, matches NASA ACRES work)
    NDMI  - Normalized Difference Moisture Index (secondary comparison)
    NDVI  - Normalized Difference Vegetation Index (greenness context)

This is the "baseline to beat." When the OlmoEarth LFMC model comes online,
we compare its output against these simple spectral indices to show what the
learned model actually adds.

The study-area boundary is read from a single shared file (aoi/corridor.geojson)
so this script, the climate-grid clip, and ArcGIS all use the SAME boundary.

Author : Samuel Dameg (SammyCode002)
Project: NASA ACRES Maui  (https://github.com/SammyCode002/nasa-acres-maui)

Run requirements:
    pip install earthengine-api
    earthengine authenticate          # one-time, opens a browser
Then set EE_PROJECT below to your Google Cloud / Earth Engine project id.
"""

import functools
import json
import logging
import os
import time
from datetime import date

import ee

# --------------------------------------------------------------------------- #
# Config  (the only block you should normally need to edit)
# --------------------------------------------------------------------------- #

# Your Earth Engine / Google Cloud project id. Required by modern GEE.
EE_PROJECT = "ace-shine-392702"

# Single source of truth for the study-area boundary. This script AND the
# climate-grid clip read from this same file so the boundary never drifts.
# Path is relative to the repo root (run the script from there).
AOI_PATH = "aoi/corridor.geojson"

# Fallback box, used ONLY if AOI_PATH is missing, so a first pass still runs
# before the GeoJSON exists. Kula (~3000 ft) down to Waiohuli Kai / S. Kihei.
# Order: [west, south, east, north].
AOI_FALLBACK_BBOX = [-156.470, 20.670, -156.290, 20.830]

START_YEAR, START_MONTH = 2023, 8  # August 2023 = the fire month
# End defaults to the current month so the series stays "to present".
_today = date.today()
END_YEAR, END_MONTH = _today.year, _today.month

COLLECTION_ID = "COPERNICUS/S2_SR_HARMONIZED"  # surface reflectance, harmonized
MAX_CLOUD_PCT = 60  # pre-filter: drop scenes that are mostly cloud
EXPORT_SCALE = 20  # meters. 20 m matches Sentinel-2 SWIR native res.
DRIVE_FOLDER = "nasa_acres_maui_gvmi"
MONITOR_TASKS = False  # set True to poll export status until tasks finish

# --------------------------------------------------------------------------- #
# 4x4 debug logging  (inputs, outputs, timing, status) via a decorator
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gvmi_baseline")


def _short(obj, limit=120):
    """Truncate a repr so server-side ee objects don't flood the log."""
    text = repr(obj)
    return text if len(text) <= limit else text[:limit] + "..."


def debug_log(func):
    """Log the 4 things we care about every call: inputs, outputs, timing, status.

    WHY a decorator: it wraps any function without us rewriting its body, so
    every step self-reports the same way. When something breaks at 11pm, the
    log already tells you which function, with what inputs, and how long it ran
    before it failed. No print-statement archaeology.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        logger.info(
            "[INPUT ] %s args=%s kwargs=%s", func.__name__, _short(args), _short(kwargs)
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
                "[STATUS] %s FAILED after %.3fs: %s", func.__name__, elapsed, exc
            )
            raise

    return wrapper


# --------------------------------------------------------------------------- #
# Core steps
# --------------------------------------------------------------------------- #


@debug_log
def initialize_ee(project):
    """Authenticate (if needed) and initialize the Earth Engine API.

    WHY the try/except: a fresh machine or expired token throws on Initialize.
    We catch it, run the interactive auth once, then initialize again instead
    of crashing with a cryptic stack trace.
    """
    try:
        ee.Initialize(project=project)
    except Exception:  # noqa: BLE001
        logger.warning("EE init failed, launching interactive authentication...")
        ee.Authenticate()
        ee.Initialize(project=project)
    return ee.Number(1).getInfo()  # tiny round-trip to confirm the link is live


def _extract_geometry(geojson):
    """Pull a single geometry dict out of a GeoJSON file.

    Handles the three shapes a GeoJSON can arrive in: a FeatureCollection, a
    single Feature, or a bare geometry.

    For a FeatureCollection we prefer the feature tagged properties.role == "aoi"
    because corridor.geojson also carries the cross-validation block features. We
    fall back to the first feature if nothing is tagged, so a plain one-polygon
    file still works whether it is a rectangle or the digitized watershed.
    """
    kind = geojson.get("type")
    if kind == "FeatureCollection":
        features = geojson["features"]
        aoi = next(
            (f for f in features if f.get("properties", {}).get("role") == "aoi"),
            features[0],
        )
        return aoi["geometry"]
    if kind == "Feature":
        return geojson["geometry"]
    return geojson  # already a bare geometry


@debug_log
def load_aoi(path, fallback_bbox):
    """Load the study-area boundary as an ee.Geometry.

    WHY a file instead of a hardcoded box: the GVMI maps, the climate grids, and
    ArcGIS all need to share ONE boundary. If each tool carries its own copy of
    the coordinates they drift apart, your layers stop lining up, and that
    mismatch is a quiet source of leakage. Reading from one GeoJSON keeps them
    locked together.

    Falls back to a bounding box if the file isn't there yet, so you can still
    run a first pass before aoi/corridor.geojson exists.
    """
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            geojson = json.load(handle)
        logger.info("[AOI   ] loaded boundary from %s", path)
        return ee.Geometry(_extract_geometry(geojson))

    logger.warning("[AOI   ] %s not found, using fallback bbox %s", path, fallback_bbox)
    west, south, east, north = fallback_bbox
    return ee.Geometry.Rectangle([west, south, east, north])


def mask_s2_clouds(image):
    """Drop cloud / shadow / cirrus / defective pixels using the SCL band.

    WHY SCL: Sentinel-2 surface reflectance ships a Scene Classification Layer
    that already labels each pixel. We keep vegetation, bare soil, water, and
    unclassified, and mask the rest. Moisture indices over a cloud read as
    garbage, so this keeps the monthly median honest.

    Masked SCL classes: 1 saturated/defective, 3 cloud shadow,
    8 cloud medium prob, 9 cloud high prob, 10 thin cirrus, 11 snow/ice.
    (Not decorated: it runs once per image inside a server-side .map().)
    """
    scl = image.select("SCL")
    keep = (
        scl.neq(1)
        .And(scl.neq(3))
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
    )
    return image.updateMask(keep)


def add_indices(image):
    """Add GVMI, NDMI, and NDVI bands to a Sentinel-2 image.

    WHY divide B8/B11 by 10000 only for GVMI: GVMI has additive constants
    (0.1 and 0.02) that assume reflectance in the 0-1 range. Sentinel-2 SR is
    stored as integers scaled by 10000, so we rescale first or the constants
    are meaningless. NDMI/NDVI are pure ratios, so scaling cancels out and
    normalizedDifference can use the raw bands directly.

    Bands used (Sentinel-2):
        B8  = NIR (842 nm), B11 = SWIR (1610 nm), B4 = Red (665 nm)
    """
    nir = image.select("B8").divide(10000)
    swir = image.select("B11").divide(10000)

    # GVMI (Ceccato et al. 2002): sensitive to vegetation water content.
    gvmi = image.expression(
        "((NIR + 0.1) - (SWIR + 0.02)) / ((NIR + 0.1) + (SWIR + 0.02))",
        {"NIR": nir, "SWIR": swir},
    ).rename("GVMI")

    ndmi = image.normalizedDifference(["B8", "B11"]).rename("NDMI")
    ndvi = image.normalizedDifference(["B8", "B4"]).rename("NDVI")

    return image.addBands([gvmi, ndmi, ndvi])


@debug_log
def load_collection(collection_id, aoi, start, end, max_cloud_pct):
    """Build the cleaned, index-enriched Sentinel-2 collection for the AOI."""
    return (
        ee.ImageCollection(collection_id)
        .filterBounds(aoi)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", max_cloud_pct))
        .map(mask_s2_clouds)
        .map(add_indices)
    )


def month_iter(start_year, start_month, end_year, end_month):
    """Yield (year, month) from start to end inclusive. Plain Python, no GEE."""
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        yield y, m
        m += 1
        if m > 12:
            m, y = 1, y + 1


@debug_log
def monthly_composite(collection, year, month, aoi):
    """Median composite of the index bands for one month, clipped to the AOI.

    WHY median: a single date is often cloud-gapped. Taking the per-pixel
    median across all clear scenes in the month fills holes and shrugs off the
    odd bad pixel that slipped past the mask.

    Returns (composite_image, scene_count). Count = 0 means no clear data.
    """
    start = ee.Date.fromYMD(year, month, 1)
    end = start.advance(1, "month")
    monthly = collection.filterDate(start, end)
    count = monthly.size().getInfo()  # one small server call so we can skip empties
    composite = monthly.select(["GVMI", "NDMI", "NDVI"]).median().clip(aoi)
    return composite, count


@debug_log
def export_to_drive(image, name, aoi, folder, scale):
    """Kick off a Drive export task for one monthly composite.

    WHY toFloat + maxPixels: index values are fractional (-1..1), so we cast to
    float to avoid integer truncation, and raise maxPixels so a multi-year run
    over the corridor doesn't trip the default pixel cap.
    """
    task = ee.batch.Export.image.toDrive(
        image=image.toFloat(),
        description=name,
        folder=folder,
        fileNamePrefix=name,
        region=aoi,
        scale=scale,
        crs="EPSG:4326",
        maxPixels=int(1e13),
    )
    task.start()
    return task


@debug_log
def monitor(tasks, poll_seconds=30):
    """Optional: poll until every export task finishes. Off by default."""
    pending = {t.id: t for t in tasks}
    while pending:
        time.sleep(poll_seconds)
        for task_id, task in list(pending.items()):
            state = task.status().get("state")
            logger.info("[MONITOR] %s -> %s", task_id, state)
            if state in ("COMPLETED", "FAILED", "CANCELLED"):
                pending.pop(task_id)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def main():
    """Generate and export one monthly index composite per month in range.

    Success criteria:
      - one GeoTIFF per month with clear data lands in DRIVE_FOLDER
      - each file has 3 float bands: GVMI, NDMI, NDVI
      - GVMI values fall within roughly [-1, 1]
      - months with no clear scenes are skipped and logged, not exported
    """
    initialize_ee(EE_PROJECT)
    aoi = load_aoi(AOI_PATH, AOI_FALLBACK_BBOX)

    start = ee.Date.fromYMD(START_YEAR, START_MONTH, 1)
    end = ee.Date.fromYMD(END_YEAR, END_MONTH, 1).advance(1, "month")
    collection = load_collection(COLLECTION_ID, aoi, start, end, MAX_CLOUD_PCT)

    tasks = []
    skipped = []
    for year, month in month_iter(START_YEAR, START_MONTH, END_YEAR, END_MONTH):
        composite, count = monthly_composite(collection, year, month, aoi)
        name = f"GVMI_{year}_{month:02d}"
        if count == 0:
            logger.warning("[SKIP  ] %s has no clear scenes, not exporting", name)
            skipped.append(name)
            continue
        task = export_to_drive(composite, name, aoi, DRIVE_FOLDER, EXPORT_SCALE)
        logger.info("[QUEUED] %s (%d clear scenes) task=%s", name, count, task.id)
        tasks.append(task)

    logger.info(
        "[SUMMARY] queued %d exports, skipped %d months -> Drive/%s",
        len(tasks),
        len(skipped),
        DRIVE_FOLDER,
    )
    logger.info(
        "[SUMMARY] watch progress at https://code.earthengine.google.com "
        "(Tasks tab) or set MONITOR_TASKS=True"
    )

    if MONITOR_TASKS and tasks:
        monitor(tasks)


if __name__ == "__main__":
    main()
