"""
scoring_aggregation.py -- interpolates GSV streetview walkability point scores
(overall_score, from the public-realm-ai project's per-BIA
streetview_analysis.geojson) onto this repo's own road centerlines, so
walkability can be visualized on the same network geometry as the NAIN/NACH
segment maps.

For each BIA: reprojects the streetview points and this repo's own
<slug>_centerlines.gpkg to TORONTO_CRS, densifies the road network every
DENSIFY_INTERVAL metres, IDW-interpolates overall_score onto a raster grid,
drapes the densified roads over that raster (Z = interpolated score), then
extracts per-feature Z statistics (walkability_mean etc.) plus a z-score.
Both the interpolated network and the reprojected points are written to
outputs/<slug>/<slug>_streetview_scores.gpkg (layers "streetview_network" and
"streetview_points") for modules/visualization.py to style and export.

Ported from public-realm-ai's modules/linear_heatmap_vis_workflow.py
(single-BIA prototype) -- same IDW/densify/drape/extract steps, generalized
to loop over every BIA and to read/write this repo's own file layout instead
of hardcoded paths.

Usage (must be run with QGIS's own Python, not the pipeline's venv -- this
script uses the QGIS Processing framework):

    & "C:\\Program Files\\QGIS 3.44.7\\bin\\python-qgis.bat" modules\\scoring_aggregation.py --bia "Downtown Yonge"
    & "C:\\Program Files\\QGIS 3.44.7\\bin\\python-qgis.bat" modules\\scoring_aggregation.py --all-bias
    & "C:\\Program Files\\QGIS 3.44.7\\bin\\python-qgis.bat" modules\\scoring_aggregation.py --list-bias

Set LIMIT_TO below to a list of BIA names to process only those (for
testing); leave empty to process all BIAs that have streetview data.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
from pathlib import Path

ANALYSIS_DIR = Path(__file__).resolve().parent.parent          # spacesyntax-analysis (repo root)
OUTPUT_ROOT  = ANALYSIS_DIR / "outputs"
BIAS_GEOJSON = ANALYSIS_DIR / "inputs" / "all bia boundaries.geojson"

# The GSV vision-analysis project this repo pulls streetview point scores
# from -- streetview_analysis.geojson is generated there by its own
# modules/scoring_aggregation/scoring_aggregation.py, one file per BIA slug
# under outputs/<slug>/.
PUBLIC_REALM_OUTPUTS = Path(
    r"C:\Users\menta\OneDrive - University of Toronto\udsc cui\walkability playbook\gsv\public-realm-ai\outputs"
)

TORONTO_CRS      = "EPSG:26917"   # matches extract_bia_boundaries.py / citywide_match.py
SCORE_FIELD      = "overall_score"
DENSIFY_INTERVAL = 4.0            # metres
IDW_PIXEL_SIZE   = 4.0            # metres
IDW_POWER        = 2.0

LIMIT_TO: list[str] = []


def slugify(name: str) -> str:
    return name.strip().replace(" ", "_").replace("/", "-")


def get_bia_names() -> list[str]:
    from qgis.core import QgsVectorLayer
    lyr = QgsVectorLayer(str(BIAS_GEOJSON), "bia_boundaries", "ogr")
    if not lyr.isValid():
        sys.exit(f"ERROR: could not open {BIAS_GEOJSON}")
    return sorted({f["AREA_NAME"] for f in lyr.getFeatures() if f["AREA_NAME"]})


def list_bias() -> None:
    print("Available BIAs:")
    for name in get_bia_names():
        print(f"  {name}")


def run_one(bia_name: str) -> bool:
    """
    Interpolate streetview walkability scores onto this BIA's road
    centerlines and write outputs/<slug>/<slug>_streetview_scores.gpkg
    (layers "streetview_network", "streetview_points").

    Returns True on success, False if inputs are missing or the step failed
    -- never raises, so --all-bias can skip BIAs without streetview data.
    """
    import processing
    from qgis.PyQt.QtCore import QVariant
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsField,
        QgsVectorFileWriter,
        QgsVectorLayer,
    )

    slug        = slugify(bia_name)
    points_path = PUBLIC_REALM_OUTPUTS / slug / "streetview_analysis.geojson"
    roads_path  = OUTPUT_ROOT / slug / f"{slug}_centerlines.gpkg"
    out_gpkg    = OUTPUT_ROOT / slug / f"{slug}_streetview_scores.gpkg"

    if not points_path.exists():
        print(f"  SKIP [{bia_name}]: no streetview_analysis.geojson at {points_path}")
        return False
    if not roads_path.exists():
        print(f"  SKIP [{bia_name}]: no centerlines at {roads_path} (run main.py for this BIA first)")
        return False

    lyr_points_raw = QgsVectorLayer(str(points_path), "streetview_points_raw", "ogr")
    lyr_roads_raw  = QgsVectorLayer(str(roads_path), "centerlines_raw", "ogr")
    if not lyr_points_raw.isValid() or not lyr_roads_raw.isValid():
        print(f"  SKIP [{bia_name}]: could not load points/roads")
        return False
    if lyr_points_raw.featureCount() == 0:
        print(f"  SKIP [{bia_name}]: streetview_analysis.geojson has no points")
        return False

    crs_target = QgsCoordinateReferenceSystem(TORONTO_CRS)

    lyr_points = processing.run("native:reprojectlayer", {
        "INPUT": lyr_points_raw, "TARGET_CRS": crs_target, "OUTPUT": "TEMPORARY_OUTPUT",
    })["OUTPUT"]
    lyr_roads = processing.run("native:reprojectlayer", {
        "INPUT": lyr_roads_raw, "TARGET_CRS": crs_target, "OUTPUT": "TEMPORARY_OUTPUT",
    })["OUTPUT"]

    if lyr_points.fields().indexOf(SCORE_FIELD) == -1:
        print(f"  SKIP [{bia_name}]: field '{SCORE_FIELD}' not found on streetview points")
        return False

    lyr_roads_dense = processing.run("native:densifygeometriesgivenaninterval", {
        "INPUT": lyr_roads, "INTERVAL": DENSIFY_INTERVAL, "OUTPUT": "TEMPORARY_OUTPUT",
    })["OUTPUT"]

    # Raster extent = the road network's own extent (already clipped to this
    # BIA + buffer by extract_bia_boundaries.py) -- IDW has global support
    # here (RADIUS 0 = unlimited), so every road vertex still gets a value
    # even where it falls far from the nearest streetview point.
    extent = lyr_roads_dense.extent()
    cols = max(1, int(extent.width() / IDW_PIXEL_SIZE))
    rows = max(1, int(extent.height() / IDW_PIXEL_SIZE))

    idw_path = str(Path(tempfile.gettempdir()) / f"{slug}_walkability_idw.tif")
    raster_idw = processing.run("gdal:gridinversedistance", {
        "INPUT": lyr_points,
        "Z_FIELD": SCORE_FIELD,
        "POWER": IDW_POWER,
        "SMOOTHING": 0.0,
        "RADIUS_1": 0, "RADIUS_2": 0,
        "MAX_POINTS": 0, "MIN_POINTS": 0,
        "ANGLE": 0,
        "NODATA": -9999,
        "DATA_TYPE": 5,
        "EXTRA": (
            f"-txe {extent.xMinimum()} {extent.xMaximum()} "
            f"-tye {extent.yMinimum()} {extent.yMaximum()} "
            f"-outsize {cols} {rows}"
        ),
        "OUTPUT": idw_path,
    })["OUTPUT"]

    lyr_roads_3d = processing.run("native:setzfromraster", {
        "INPUT": lyr_roads_dense, "RASTER": raster_idw, "BAND": 1, "SCALE": 1,
        "OUTPUT": "TEMPORARY_OUTPUT",
    })["OUTPUT"]

    lyr_network = processing.run("native:extractzvalues", {
        "INPUT": lyr_roads_3d,
        "SUMMARIES": [0, 1, 2, 3, 4, 5, 6],  # count, sum, mean, median, stdev, min, max
        "COLUMN_PREFIX": "walkability_",
        "OUTPUT": "TEMPORARY_OUTPUT",
    })["OUTPUT"]

    means = [f["walkability_mean"] for f in lyr_network.getFeatures() if f["walkability_mean"] is not None]
    if not means:
        print(f"  SKIP [{bia_name}]: no walkability_mean values (IDW raster may not overlap roads)")
        return False
    mean  = statistics.mean(means)
    stdev = statistics.stdev(means) if len(means) > 1 else 0.0

    lyr_network.startEditing()
    lyr_network.addAttribute(QgsField("walkability_zscore", QVariant.Double))
    lyr_network.updateFields()
    zscore_idx = lyr_network.fields().indexOf("walkability_zscore")
    for f in lyr_network.getFeatures():
        val = f["walkability_mean"]
        if val is not None and stdev > 0:
            lyr_network.changeAttributeValue(f.id(), zscore_idx, (val - mean) / stdev)
    lyr_network.commitChanges()

    lyr_network.setName("streetview_network")
    lyr_points.setName("streetview_points")

    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if out_gpkg.exists():
        try:
            out_gpkg.unlink()
        except PermissionError:
            print(f"  WARNING: {out_gpkg.name} is locked (open in QGIS?) -- overwriting in place")

    for i, lyr in enumerate((lyr_network, lyr_points)):
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName  = lyr.name()
        if i > 0:
            opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
        err = QgsVectorFileWriter.writeAsVectorFormatV3(
            lyr, str(out_gpkg), lyr.transformContext(), opts,
        )
        if err[0] != QgsVectorFileWriter.NoError:
            print(f"  ERROR [{bia_name}]: failed writing layer '{lyr.name()}' -- {err}")
            return False

    print(f"  OK   [{bia_name}]: {lyr_network.featureCount():,} network features, "
          f"{lyr_points.featureCount():,} points  (mean={mean:.2f}, stdev={stdev:.2f}) "
          f"-> {out_gpkg.name}")
    return True


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--bia", help="BIA name, e.g. 'Downtown Yonge'")
    g.add_argument("--all-bias", action="store_true", help="Run for every BIA with streetview data")
    g.add_argument("--list-bias", action="store_true", help="Print available BIA names and exit")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.list_bias:
        list_bias()
        sys.exit(0)
    if not args.bia and not args.all_bias:
        sys.exit("ERROR: --bia, --all-bias, or --list-bias is required.")

    from qgis.core import QgsApplication
    qgs = QgsApplication([], False)
    qgs.initQgis()

    # The Processing plugin is bundled inside QGIS's own install (not on
    # sys.path for standalone scripts run via python-qgis.bat) --
    # <QGIS prefix>/python/plugins holds it alongside QgsApplication's own
    # prefixPath.
    plugins_dir = str(Path(QgsApplication.prefixPath()) / "python" / "plugins")
    if plugins_dir not in sys.path:
        sys.path.append(plugins_dir)
    from processing.core.Processing import Processing
    Processing.initialize()

    if args.all_bias:
        names = LIMIT_TO if LIMIT_TO else get_bia_names()
        print(f"Running scoring aggregation for {len(names)} BIAs...\n")
        ok, skipped = 0, 0
        for i, name in enumerate(names, 1):
            print(f"[{i}/{len(names)}] {name}")
            if run_one(name):
                ok += 1
            else:
                skipped += 1
        print(f"\nDone. {ok} built, {skipped} skipped.")
    else:
        if not run_one(args.bia):
            qgs.exitQgis()
            sys.exit(1)

    qgs.exitQgis()


if __name__ == "__main__":
    main()
