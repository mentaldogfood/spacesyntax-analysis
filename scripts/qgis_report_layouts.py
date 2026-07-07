"""
qgis_report_layouts.py -- build map layout PNGs for each BIA.

For every BIA, for each requested map type (integration / choice / segment length): loads the BIA's gpkg as a
project layer, applies that map type's shared style, then creates/replaces a named print
layout with:
  - a map item pinned to that BIA's layer + basemap, filling the full page
    (297x210mm), zoomed to the extent of that BIA's centerlines
  - a legend
  - a title label showing the BIA name, map type, and its rank for that metric
Then exports each layout to outputs/<slug>/<slug>_<maptype>_print_map.png.

Usage (must be run with QGIS's own Python, not the pipeline's venv):
    "C:\\Program Files\\QGIS <version>\\bin\\python-qgis.bat" scripts\\qgis_report_layouts.py
    (the exact bin name depends on your install -- python-qgis-ltr.bat for the LTR line)

    --maps all                       (default) export all three map types
    --maps integration                export only the integration maps
    --maps choice segment_length      export choice + segment length maps

Set LIMIT_TO below to a list of BIA names to process only those (for testing);
leave empty to process all BIAs.

Requires "spatial syntax processing.qgz" (in the repo root)
"""
from __future__ import annotations

import argparse
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

CONTEXT_LAYER_NAMES = ["VersaTiles Graybeard copy"]
MARGIN_FRACTION      = 0.03  # extra space around each BIA's centerlines extent
PAGE_WIDTH_MM        = 297.0
PAGE_HEIGHT_MM       = 210.0
EXPORT_DPI           = 300

# One entry per exportable map type. style_source_layer's renderer is cloned
# onto each BIA's data layer (the actual map colours); legend_source_layer is
# a standalone reference layer used only to draw the on-page Legend, so every
# BIA's page for a given map type is pixel-identical there. rank_col is the
# precomputed "<metric>_rank" column in outputs/all_bia_summary.csv (1 = best;
# main.py already accounts for segment length ranking lower-is-better) used
# for the "(n/86)" title suffix.
# sparse_labels=True blanks every bin except the first/last ("Low"/"High");
# False leaves the layer's own per-bin labels untouched (Segment Length's
# bins are already labelled with real metre ranges in the qgz).
MAP_TYPE_DEFS = {
    "integration": dict(
        style_source_layer="Integration",
        legend_source_layer="Integration",
        rank_col="avg_blended_nain_rank",
        label="Integration",
        sparse_labels=True,
    ),
    "choice": dict(
        style_source_layer="Choice",
        legend_source_layer="Choice",
        rank_col="avg_blended_nach_rank",
        label="Choice",
        sparse_labels=True,
    ),
    "segment_length": dict(
        style_source_layer="Segment Length",
        legend_source_layer="Segment Length",
        rank_col="avg_segment_length_rank",
        label="Segment Length",
        sparse_labels=False,
    ),
}

# Set to e.g. ["Downtown Yonge", "Korea Town", "Wilson Village"] to test a
# handful of BIAs only. Leave as [] to process all.
LIMIT_TO: list[str] = []
_KEEP_ALIVE: list = []


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument(
        "--maps", nargs="+", choices=[*MAP_TYPE_DEFS, "all"], default=["all"],
        help="Which map type(s) to export (default: all)",
    )
    return p.parse_args()


def get_bia_names() -> list[str]:
    lyr = QgsVectorLayer(str(BIAS_GEOJSON), "bia_boundaries", "ogr")
    if not lyr.isValid():
        sys.exit(f"ERROR: could not open {BIAS_GEOJSON}")
    names = sorted({f["AREA_NAME"] for f in lyr.getFeatures() if f["AREA_NAME"]})
    return names


def slugify(name: str) -> str:
    return name.strip().replace(" ", "_").replace("/", "-")


def load_summary_rows() -> list[dict]:
    summary_csv = OUTPUT_ROOT / "all_bia_summary.csv"
    with open(summary_csv, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def rank_lookup(rows: list[dict], col: str) -> tuple[dict[str, int], int]:
    """Return ({bia_name: rank}, total) reading the precomputed rank column
    `col` (1 = best) straight from all_bia_summary.csv."""
    ranks = {row["bia"]: int(row[col]) for row in rows}
    return ranks, len(rows)


def _find_by_source(project: QgsProject, gpkg: Path):
    target = str(gpkg).replace("\\", "/")
    for lyr in project.mapLayers().values():
        src = lyr.source().split("|")[0].replace("\\", "/")
        if src == target:
            return lyr
    return None


def ensure_layer(project: QgsProject, slug: str) -> QgsVectorLayer | None:
    """Load (or reload) this BIA's scored layer into the project. Caller is
    responsible for setting a renderer before rendering/exporting it."""
    gpkg = OUTPUT_ROOT / slug / f"{slug}_segment_scores.gpkg"
    if not gpkg.exists():
        return None

    old = _find_by_source(project, gpkg)
    if old is not None:
        project.removeMapLayer(old.id())

    lyr = QgsVectorLayer(str(gpkg), f"{slug}_segment_scores", "ogr")
    if not lyr.isValid():
        return None
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


def build_layout(project: QgsProject, layout_name: str, title_text: str, slug: str,
                  data_layer: QgsVectorLayer, context_layers: list,
                  legend_source_layer: QgsVectorLayer, sparse_labels: bool) -> QgsPrintLayout:
    manager = project.layoutManager()
    old = manager.layoutByName(layout_name)
    if old is not None:
        manager.removeLayout(old)

    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(layout_name)
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
    title.setText(title_text)
    tf = QgsTextFormat()
    font = tf.font()
    font.setBold(True)
    tf.setFont(font)
    tf.setSize(20)
    title.setTextFormat(tf)
    title.attemptMove(QgsLayoutPoint(8, 5, QgsUnitTypes.LayoutMillimeters))
    title.attemptResize(QgsLayoutSize(PAGE_WIDTH_MM - 16, 12, QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(title)

    # -- Legend: always the standalone reference layer's own gradient, never
    # the per-BIA data layer -- so every page for this map type is
    # pixel-identical here. No separate item-level title -- the layer's own
    # name is already the heading; a second title would just duplicate it.
    legend = QgsLayoutItemLegend(layout)
    legend.setLinkedMap(map_item)
    legend.setTitle("")
    legend.setAutoUpdateModel(False)
    root = QgsLayerTree()
    lt_layer = root.addLayer(legend_source_layer)
    legend.model().setRootGroup(root)
    _KEEP_ALIVE.append(root)

    # sparse_labels: blank all bins but the first/last ("Low"/"High"), for
    # the continuous-gradient look. Otherwise leave the layer's own per-bin
    # labels as configured in the qgz (e.g. Segment Length's metre ranges).
    if sparse_labels:
        node_count = len(legend.model().layerLegendNodes(lt_layer))
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


def resolve_map_types(project: QgsProject, keys: list[str], summary_rows: list[dict]) -> dict:
    """Resolve each requested map type's style renderer, legend layer, and
    per-metric ranking up front (once), so the per-BIA loop can just clone."""
    resolved = {}
    for key in keys:
        cfg = MAP_TYPE_DEFS[key]

        style_layers = project.mapLayersByName(cfg["style_source_layer"])
        if not style_layers:
            sys.exit(f"ERROR: style-source layer '{cfg['style_source_layer']}' not found "
                      f"in project (map type: {key})")
        style_layer = style_layers[0]

        legend_layers = project.mapLayersByName(cfg["legend_source_layer"])
        if not legend_layers:
            sys.exit(f"ERROR: legend-source layer '{cfg['legend_source_layer']}' not found "
                      f"in project (map type: {key})")

        ranks, total = rank_lookup(summary_rows, cfg["rank_col"])
        resolved[key] = dict(
            style_renderer=style_layer.renderer().clone(),
            legend_layer=legend_layers[0],
            label=cfg["label"],
            sparse_labels=cfg["sparse_labels"],
            ranks=ranks,
            total=total,
        )
    return resolved


def main() -> None:
    args = parse_args()
    selected = list(MAP_TYPE_DEFS) if "all" in args.maps else list(dict.fromkeys(args.maps))

    qgs = QgsApplication([], False)
    qgs.initQgis()

    project = QgsProject.instance()
    if not project.read(str(PROJECT_PATH)):
        sys.exit(f"ERROR: could not open project {PROJECT_PATH}")
    print(f"Opened project: {PROJECT_PATH}")

    context_layers = []
    for cname in CONTEXT_LAYER_NAMES:
        found = project.mapLayersByName(cname)
        if found:
            context_layers.append(found[0])
        else:
            print(f"  WARNING: context layer not found, skipping: {cname}")

    summary_rows = load_summary_rows()
    resolved = resolve_map_types(project, selected, summary_rows)

    names = LIMIT_TO if LIMIT_TO else get_bia_names()
    print(f"{len(names)} BIAs x {len(selected)} map type(s) "
          f"({', '.join(selected)}) = {len(names) * len(selected)} exports\n", flush=True)

    built, skipped, failed = 0, [], []
    for i, name in enumerate(names, 1):
        slug = slugify(name)
        lyr = ensure_layer(project, slug)
        if lyr is None:
            skipped.append(name)
            print(f"  [{i}/{len(names)}] SKIP (no gpkg): {name}", flush=True)
            continue

        for key in selected:
            cfg = resolved[key]
            try:
                lyr.setRenderer(cfg["style_renderer"].clone())
                rank = cfg["ranks"].get(name)
                rank_suffix = f"({rank}/{cfg['total']})" if rank is not None else ""
                title_text = f"{name} — {cfg['label']} {rank_suffix}".strip()
                layout = build_layout(project, f"{name} — {cfg['label']}", title_text, slug,
                                       lyr, context_layers, cfg["legend_layer"], cfg["sparse_labels"])
                png_path = OUTPUT_ROOT / slug / f"{slug}_{key}_print_map.png"
                ok = export_png(layout, png_path)
                built += 1
                print(f"  [{i}/{len(names)}] {key}: {'ok' if ok else 'EXPORT FAILED'} "
                      f"-> {png_path.name}", flush=True)
            except Exception as exc:
                failed.append(f"{name} ({key})")
                print(f"  [{i}/{len(names)}] {key}: FAILED -- {exc}", flush=True)

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
