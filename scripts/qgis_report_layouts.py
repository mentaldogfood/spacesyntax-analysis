"""
qgis_report_layouts.py -- build one print-quality report layout + PNG per BIA.

For every BIA: loads in the geopackage as a project layer, applies a style, then creates a named print layout with:
  - a map item with base vector tiles and the BIA's centerlines filling the full page
  - a legend for the integration value, from low (0) to high (2)
  - a title label showing the BIA name
Then exports each layout to outputs/BIAName_print_map.png.

Set LIMIT_TO below to a list of BIA names to process only those (for testing);
leave empty to process all BIAs.

Requires "spatial syntax processing.qgz" which is included in the repo
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

from qgis.core import (
    QgsApplication,
    QgsLayerTree,
    QgsLayoutExporter,
    QgsLayoutItem,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsMapLayerLegendUtils,
    QgsPrintLayout,
    QgsProject,
    QgsRectangle,
    QgsTextFormat,
    QgsUnitTypes,
    QgsVectorLayer,
)

ANALYSIS_DIR   = Path(__file__).resolve().parent.parent          # spacesyntax-analysis (repo root)
PROJECT_PATH   = ANALYSIS_DIR / "spatial syntax processing.qgz"
OUTPUT_ROOT    = ANALYSIS_DIR / "outputs"
BIAS_GEOJSON   = ANALYSIS_DIR / "inputs" / "all bia boundaries.geojson"

MAP_STYLE_SOURCE_LAYER = "Downtown_Yonge_segment_scores"  # renderer cloned onto every BIA's map layer
LEGEND_SOURCE_LAYER    = "Integration"                     # legend content is ALWAYS this layer's
CONTEXT_LAYER_NAMES    = ["VersaTiles Graybeard copy"]
MARGIN_FRACTION     = 0.03  # extra space around each BIA's centerlines extent
PAGE_WIDTH_MM       = 297.0
PAGE_HEIGHT_MM      = 210.0
EXPORT_DPI          = 300

# Set to e.g. ["Downtown Yonge", "Korea Town", "Wilson Village"] to test a
# handful of BIAs only. Leave as [] to process all.
LIMIT_TO: list[str] = []

# Custom QgsLayerTree roots handed to legend.model().setRootGroup() may not be
# fully owned by the C++ side -- keep a Python reference alive for the whole
# run so they can't be garbage-collected out from under a legend mid-script.
_KEEP_ALIVE: list = []


def get_bia_names() -> list[str]:
    lyr = QgsVectorLayer(str(BIAS_GEOJSON), "bia_boundaries", "ogr")
    if not lyr.isValid():
        sys.exit(f"ERROR: could not open {BIAS_GEOJSON}")
    names = sorted({f["AREA_NAME"] for f in lyr.getFeatures() if f["AREA_NAME"]})
    return names


def slugify(name: str) -> str:
    return name.strip().replace(" ", "_").replace("/", "-")


def load_rankings() -> tuple[dict[str, int], int]:
    """Return ({bia_name: rank}, total) ranked by avg_blended_nain, 1 = highest."""
    summary_csv = OUTPUT_ROOT / "all_bia_summary.csv"
    rows = []
    with open(summary_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append((row["bia"], float(row["avg_blended_nain"])))
    rows.sort(key=lambda r: r[1], reverse=True)
    ranks = {name: i + 1 for i, (name, _) in enumerate(rows)}
    return ranks, len(rows)


def _find_by_source(project: QgsProject, gpkg: Path):
    target = str(gpkg).replace("\\", "/")
    for lyr in project.mapLayers().values():
        src = lyr.source().split("|")[0].replace("\\", "/")
        if src == target:
            return lyr
    return None


def ensure_layer(project: QgsProject, name: str, slug: str, style_renderer) -> QgsVectorLayer | None:
    gpkg = OUTPUT_ROOT / slug / f"{slug}_segment_scores.gpkg"
    if not gpkg.exists():
        print(f"  SKIP (no gpkg): {name}")
        return None

    old = _find_by_source(project, gpkg)
    if old is not None:
        project.removeMapLayer(old.id())

    lyr = QgsVectorLayer(str(gpkg), f"{slug}_segment_scores", "ogr")
    if not lyr.isValid():
        print(f"  SKIP (invalid layer): {name}")
        return None
    lyr.setRenderer(style_renderer.clone())
    project.addMapLayer(lyr)
    return lyr


def centerlines_extent(slug: str) -> QgsRectangle | None:
    gpkg = OUTPUT_ROOT / slug / f"{slug}_centerlines.gpkg"
    if not gpkg.exists():
        return None
    lyr = QgsVectorLayer(str(gpkg), "centerlines", "ogr")
    if not lyr.isValid():
        return None
    return lyr.extent()


def build_layout(project: QgsProject, bia_name: str, slug: str, data_layer: QgsVectorLayer,
                  context_layers: list, legend_source_layer: QgsVectorLayer,
                  rank_suffix: str = "") -> QgsPrintLayout:
    manager = project.layoutManager()
    old = manager.layoutByName(bia_name)
    if old is not None:
        manager.removeLayout(old)

    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(bia_name)
    page = layout.pageCollection().page(0)
    page.setPageSize(QgsLayoutSize(PAGE_WIDTH_MM, PAGE_HEIGHT_MM, QgsUnitTypes.LayoutMillimeters))

    # -- Map item, exactly one page in size, no more/less --------------------
    map_item = QgsLayoutItemMap(layout)
    map_item.attemptMove(QgsLayoutPoint(0, 0, QgsUnitTypes.LayoutMillimeters))
    map_item.attemptResize(QgsLayoutSize(PAGE_WIDTH_MM, PAGE_HEIGHT_MM, QgsUnitTypes.LayoutMillimeters))
    map_item.setLayers([data_layer] + context_layers)
    map_item.setKeepLayerSet(True)
    map_item.setCrs(data_layer.crs())
    layout.addLayoutItem(map_item)

    extent = centerlines_extent(slug) or data_layer.extent()
    dx = extent.width() * MARGIN_FRACTION
    dy = extent.height() * MARGIN_FRACTION
    extent.grow(max(dx, dy) if max(dx, dy) > 0 else 1.0)
    map_item.zoomToExtent(extent)  # aspect-preserving fit within the map frame

    # -- Title label -----------------------------------------------------
    title = QgsLayoutItemLabel(layout)
    title.setText(f"{bia_name} {rank_suffix}".strip())
    tf = QgsTextFormat()
    font = tf.font()
    font.setBold(True)
    tf.setFont(font)
    tf.setSize(20)
    title.setTextFormat(tf)
    title.attemptMove(QgsLayoutPoint(8, 5, QgsUnitTypes.LayoutMillimeters))
    title.attemptResize(QgsLayoutSize(PAGE_WIDTH_MM - 16, 12, QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(title)

    # -- Legend: always the standalone "Integration" layer's own gradient,
    # never the per-BIA data layer -- so every page is pixel-identical here.
    # All bins stay visible (for the continuous-gradient look); only the
    # first ("Low") and last ("High") get a text label, rest are blanked.
    # No separate item-level title -- the layer's own name ("Integration")
    # is already the heading; a second title would just duplicate it.
    legend = QgsLayoutItemLegend(layout)
    legend.setLinkedMap(map_item)
    legend.setTitle("")
    legend.setAutoUpdateModel(False)
    root = QgsLayerTree()
    lt_layer = root.addLayer(legend_source_layer)
    legend.model().setRootGroup(root)
    _KEEP_ALIVE.append(root)

    node_count = len(legend.model().layerLegendNodes(lt_layer))
    if node_count >= 1:
        for idx in range(node_count):
            if idx == 0:
                label = "Low"
            elif idx == node_count - 1:
                label = "High"
            else:
                label = ""
            QgsMapLayerLegendUtils.setLegendNodeUserLabel(lt_layer, idx, label)
        legend.model().refreshLayerLegend(lt_layer)

    legend.setSymbolHeight(2.5)
    legend.setSymbolWidth(8)
    # QgsLayoutItemLegend auto-resizes to fit all rows regardless of the size
    # requested below -- anchor by the LOWER-left corner so it grows upward
    # into free space instead of downward past the page edge.
    legend.setReferencePoint(QgsLayoutItem.ReferencePoint.LowerLeft)
    legend.attemptMove(QgsLayoutPoint(8, PAGE_HEIGHT_MM - 8, QgsUnitTypes.LayoutMillimeters))
    legend.attemptResize(QgsLayoutSize(45, 50, QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(legend)

    manager.addLayout(layout)
    return layout


def export_png(layout: QgsPrintLayout, out_path: Path) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    exporter = QgsLayoutExporter(layout)
    settings = QgsLayoutExporter.ImageExportSettings()
    settings.dpi = EXPORT_DPI
    result = exporter.exportToImage(str(out_path), settings)
    return result == QgsLayoutExporter.Success


def main() -> None:
    qgs = QgsApplication([], False)
    qgs.initQgis()

    project = QgsProject.instance()
    if not project.read(str(PROJECT_PATH)):
        sys.exit(f"ERROR: could not open project {PROJECT_PATH}")
    print(f"Opened project: {PROJECT_PATH}")

    style_source_slug = slugify(MAP_STYLE_SOURCE_LAYER.removesuffix("_segment_scores"))
    style_source_gpkg = OUTPUT_ROOT / style_source_slug / f"{style_source_slug}_segment_scores.gpkg"
    style_layer = project.mapLayersByName(MAP_STYLE_SOURCE_LAYER)
    style_layer = style_layer[0] if style_layer else _find_by_source(project, style_source_gpkg)
    if style_layer is None:
        sys.exit(f"ERROR: style-source layer '{MAP_STYLE_SOURCE_LAYER}' not found in project "
                  f"(also checked source path {style_source_gpkg})")
    style_renderer = style_layer.renderer().clone()

    legend_layers = project.mapLayersByName(LEGEND_SOURCE_LAYER)
    if not legend_layers:
        sys.exit(f"ERROR: legend-source layer '{LEGEND_SOURCE_LAYER}' not found in project")
    legend_source_layer = legend_layers[0]

    context_layers = []
    for cname in CONTEXT_LAYER_NAMES:
        found = project.mapLayersByName(cname)
        if found:
            context_layers.append(found[0])
        else:
            print(f"  WARNING: context layer not found, skipping: {cname}")

    ranks, total = load_rankings()

    names = LIMIT_TO if LIMIT_TO else get_bia_names()
    print(f"{len(names)} BIAs to process\n", flush=True)

    built, skipped, failed = 0, [], []
    for i, name in enumerate(names, 1):
        slug = slugify(name)
        try:
            lyr = ensure_layer(project, name, slug, style_renderer)
            if lyr is None:
                skipped.append(name)
                continue
            rank = ranks.get(name)
            rank_suffix = f"({rank}/{total})" if rank is not None else ""
            layout = build_layout(project, name, slug, lyr, context_layers,
                                   legend_source_layer, rank_suffix)
            png_path = OUTPUT_ROOT / slug / f"{slug}_print_map.png"
            ok = export_png(layout, png_path)
            built += 1
            print(f"  [{i}/{len(names)}] {'ok' if ok else 'EXPORT FAILED'}: {name} -> {png_path.name}", flush=True)
        except Exception as exc:
            failed.append(name)
            print(f"  [{i}/{len(names)}] FAILED: {name} -- {exc}", flush=True)

        if i % 10 == 0:
            project.write(str(PROJECT_PATH))
            print("  (checkpoint saved)", flush=True)

    if not project.write(str(PROJECT_PATH)):
        sys.exit("ERROR: failed to save project")

    print(f"\nDone. {built} built, {len(skipped)} skipped, {len(failed)} failed.", flush=True)
    if skipped:
        print("Skipped (no scored gpkg found):")
        for n in skipped:
            print(f"  - {n}")
    if failed:
        print("Failed (exception during build):")
        for n in failed:
            print(f"  - {n}")

    qgs.exitQgis()


if __name__ == "__main__":
    main()
