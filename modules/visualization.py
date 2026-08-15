"""
For every BIA, for each requested map type (integration / choice / segment length): loads the BIA's gpkg as a
project layer, applies that map type's shared style, then creates/replaces a named print
layout with:
  - a map item pinned to that BIA's layer + basemap, filling the full page
    (297x210mm), zoomed to the extent of that BIA's centerlines
  - a legend
  - a title label showing the BIA name, map type, and its rank for that metric
Then exports each layout to outputs/<slug>/<slug>_<maptype>_print_map.png.

A fourth, opt-in map type -- walkability -- draws the interpolated streetview
walkability network + raw streetview points from outputs/<slug>/<slug>_
streetview_scores.gpkg (built by scoring_aggregation.py) using the same
red-to-green ramp as the "streetview_analysis" points; see
build_walkability_renderer / ensure_walkability_layers / build_walkability_layout.
Titled "<BIA> -- Public Realms (n/86)", ranked by overall_score in
public-realm-ai's own outputs/bia_walkability_summary.csv (see
load_walkability_ranks) -- and exported *into* public-realm-ai's own
outputs/<slug>/ rather than this repo's, since it's really their deliverable.

Usage (must be run with QGIS's own Python, not the pipeline's venv):

    & "C:\\Program Files\\QGIS 3.44.7\\bin\\python-qgis.bat" modules\\visualization.py

    --maps all                       (default) export integration/choice/segment_length
    --maps integration                export only the integration maps
    --maps choice segment_length      export choice + segment length maps
    --maps walkability                export only the streetview walkability maps (run
                                       scoring_aggregation.py first to build the per-BIA gpkg)
    --all-streets                     one citywide map of every raw streetview point (no
                                       interpolation, no ranking) -- runs instead of --maps,
                                       exported to public-realm-ai's outputs/All_Streets/

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
    QgsGraduatedSymbolRenderer,
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
    QgsRendererRange,
    QgsSymbol,
    QgsTextFormat,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtGui import QColor

ANALYSIS_DIR   = Path(__file__).resolve().parent.parent          # spacesyntax-analysis (repo root)
PROJECT_PATH   = ANALYSIS_DIR / "spatial syntax processing.qgz"
OUTPUT_ROOT    = ANALYSIS_DIR / "outputs"
BIAS_GEOJSON   = ANALYSIS_DIR / "inputs" / "all bia boundaries.geojson"

# The GSV vision-analysis project this repo pulls streetview walkability
# scores from -- see modules/scoring_aggregation.py. Walkability maps are
# exported *there* (not into this repo's own outputs/) since they're really
# public-realm-ai's own deliverable, just rendered with this repo's road
# network + QGIS layout machinery.
PUBLIC_REALM_OUTPUTS = Path(
    r"C:\Users\menta\OneDrive - University of Toronto\udsc cui\walkability playbook\gsv\public-realm-ai\outputs"
)
WALKABILITY_SUMMARY_CSV = PUBLIC_REALM_OUTPUTS / "bia_walkability_summary.csv"

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

# Walkability (streetview) map: built from outputs/<slug>/<slug>_streetview_
# scores.gpkg, produced by scoring_aggregation.py -- "streetview_network" is
# this BIA's road centerlines with an interpolated walkability_mean per
# feature, "streetview_points" are the raw GSV streetview points it was
# interpolated from (public-realm-ai's streetview_analysis.geojson,
# reprojected). Not part of MAP_TYPE_DEFS/--maps all since it depends on
# cross-repo data that may not be present for every BIA -- request it
# explicitly with --maps walkability.
# Breaks/colours match the "streetview_analysis" / "Overall Walkability
# Score" styling used in the public-realm-ai project (ColorBrewer RdYlGn,
# 5-class, fixed 0-5 breaks) so points and interpolated network read as the
# same colour language.
WALKABILITY_BREAKS = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
WALKABILITY_COLORS = [
    (215, 25, 28),
    (253, 174, 97),
    (255, 255, 192),
    (166, 217, 106),
    (26, 150, 65),
]
WALKABILITY_LABELS = ["0 - 1", "1 - 2", "2 - 3", "3 - 4", "4 - 5"]
WALKABILITY_NETWORK_FIELD = "walkability_mean"
WALKABILITY_POINT_FIELD   = "overall_score"

# Set to e.g. ["Downtown Yonge", "Korea Town", "Wilson Village"] to test a
# handful of BIAs only. Leave as [] to process all.
LIMIT_TO: list[str] = []
_KEEP_ALIVE: list = []


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument(
        "--maps", nargs="+", choices=[*MAP_TYPE_DEFS, "all", "walkability"], default=["all"],
        help="Which map type(s) to export (default: all; 'walkability' is opt-in, see --maps walkability)",
    )
    p.add_argument(
        "--bia", help="Export only this one BIA (overrides LIMIT_TO), e.g. 'Danforth Village'",
    )
    p.add_argument(
        "--all-streets", action="store_true",
        help="Export one citywide map of every raw streetview point (public-realm-ai's own "
             "outputs/All_Streets/streetview_analysis.geojson) -- no interpolation, no ranking, "
             "no per-BIA loop. Runs instead of --maps.",
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


def load_walkability_ranks() -> tuple[dict[str, int], int]:
    """
    Rank every BIA by overall_score in public-realm-ai's own
    bia_walkability_summary.csv (1 = best), tie-aware ("min" method: tied
    BIAs share the lowest rank in their tie group, same convention as
    main.py's RANK_COLUMNS) -- unlike the NAIN/NACH/segment-length metrics,
    this csv has no precomputed rank column, so it's computed here.
    """
    with open(WALKABILITY_SUMMARY_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    scored = sorted(
        ((row["bia"], float(row["overall_score"])) for row in rows if row.get("overall_score")),
        key=lambda t: t[1], reverse=True,
    )

    ranks: dict[str, int] = {}
    prev_score, prev_rank = None, 0
    for i, (name, score) in enumerate(scored, start=1):
        if score != prev_score:
            prev_rank, prev_score = i, score
        ranks[name] = prev_rank
    return ranks, len(scored)


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


def build_walkability_renderer(field: str, geometry: str) -> QgsGraduatedSymbolRenderer:
    """
    5-class red(low)-to-green(high) graduated renderer on `field`, fixed
    0-5 breaks -- see WALKABILITY_BREAKS/COLORS. geometry is "point" or
    "line"; used for both the raw streetview points and the interpolated
    network so they share one colour language.
    """
    wkb_type = QgsWkbTypes.PointGeometry if geometry == "point" else QgsWkbTypes.LineGeometry
    ranges = []
    for lo, hi, (r, g, b), label in zip(
        WALKABILITY_BREAKS[:-1], WALKABILITY_BREAKS[1:], WALKABILITY_COLORS, WALKABILITY_LABELS
    ):
        symbol = QgsSymbol.defaultSymbol(wkb_type)
        symbol.setColor(QColor(r, g, b))
        if geometry == "line":
            symbol.setWidth(1.2)
        else:
            symbol.setSize(2.0)
        ranges.append(QgsRendererRange(lo, hi, symbol, label))
    return QgsGraduatedSymbolRenderer(field, ranges)


def ensure_walkability_layers(project: QgsProject, slug: str) -> dict | None:
    """
    Load (or reload) this BIA's streetview_network + streetview_points
    layers from outputs/<slug>/<slug>_streetview_scores.gpkg (built by
    scoring_aggregation.py) and style both with the shared red-to-green
    ramp. Returns None if the gpkg doesn't exist for this BIA.
    """
    gpkg = OUTPUT_ROOT / slug / f"{slug}_streetview_scores.gpkg"
    if not gpkg.exists():
        return None

    for old in list(project.mapLayersByName(f"{slug}_streetview_network")) + \
               list(project.mapLayersByName(f"{slug}_streetview_points")):
        project.removeMapLayer(old.id())

    network = QgsVectorLayer(f"{gpkg}|layername=streetview_network", f"{slug}_streetview_network", "ogr")
    points  = QgsVectorLayer(f"{gpkg}|layername=streetview_points", f"{slug}_streetview_points", "ogr")
    if not network.isValid() or not points.isValid():
        return None

    network.setRenderer(build_walkability_renderer(WALKABILITY_NETWORK_FIELD, "line"))
    points.setRenderer(build_walkability_renderer(WALKABILITY_POINT_FIELD, "point"))
    project.addMapLayer(network, False)
    project.addMapLayer(points, False)
    return {"network": network, "points": points}


ALL_STREETS_POINTS = PUBLIC_REALM_OUTPUTS / "All_Streets" / "streetview_analysis.geojson"


def ensure_all_streets_points(project: QgsProject) -> QgsVectorLayer | None:
    """
    Load (or reload) the citywide raw streetview points -- no interpolation,
    no per-BIA network, just every point in public-realm-ai's own
    outputs/All_Streets/streetview_analysis.geojson styled with the same
    red-to-green ramp. Returns None if that file doesn't exist.
    """
    if not ALL_STREETS_POINTS.exists():
        return None

    for old in list(project.mapLayersByName("All_Streets_streetview_points")):
        project.removeMapLayer(old.id())

    points = QgsVectorLayer(str(ALL_STREETS_POINTS), "All_Streets_streetview_points", "ogr")
    if not points.isValid():
        return None

    points.setRenderer(build_walkability_renderer(WALKABILITY_POINT_FIELD, "point"))
    project.addMapLayer(points, False)
    return points


def build_walkability_layout(project: QgsProject, layout_name: str, title_text: str, slug: str,
                              network_layer: QgsVectorLayer | None, points_layer: QgsVectorLayer,
                              context_layers: list) -> QgsPrintLayout:
    """
    Same page layout as build_layout(), but draws the interpolated network
    on top of the basemap with the raw streetview points overlaid above it
    -- both sharing the walkability renderer's colours. Only the network
    layer feeds the on-page legend (the points would just duplicate the
    same five colour bins).

    network_layer may be None for a points-only render (e.g. the citywide
    All Streets map, which skips scoring_aggregation.py's interpolation
    entirely) -- in that case points_layer alone drives the map, extent, and
    legend.
    """
    manager = project.layoutManager()
    old = manager.layoutByName(layout_name)
    if old is not None:
        manager.removeLayout(old)

    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(layout_name)
    page = layout.pageCollection().page(0)
    page.setPageSize(QgsLayoutSize(PAGE_WIDTH_MM, PAGE_HEIGHT_MM, QgsUnitTypes.LayoutMillimeters))

    legend_layer = network_layer if network_layer is not None else points_layer

    map_item = QgsLayoutItemMap(layout)
    map_item.attemptMove(QgsLayoutPoint(0, 0, QgsUnitTypes.LayoutMillimeters))
    map_item.attemptResize(QgsLayoutSize(PAGE_WIDTH_MM, PAGE_HEIGHT_MM, QgsUnitTypes.LayoutMillimeters))
    map_layers = [points_layer, network_layer] if network_layer is not None else [points_layer]
    map_item.setLayers(map_layers + context_layers)
    map_item.setKeepLayerSet(True)
    map_item.setCrs(legend_layer.crs())
    layout.addLayoutItem(map_item)

    extent = centerlines_extent(slug) or legend_layer.extent()
    dx = extent.width() * MARGIN_FRACTION
    dy = extent.height() * MARGIN_FRACTION
    extent.grow(max(dx, dy) if max(dx, dy) > 0 else 1.0)
    map_item.zoomToExtent(extent)

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

    legend = QgsLayoutItemLegend(layout)
    legend.setLinkedMap(map_item)
    legend.setTitle("")
    legend.setAutoUpdateModel(False)
    root = QgsLayerTree()
    lt_layer = root.addLayer(legend_layer)
    # legend_layer's own name is a technical name used to find/remove it
    # across BIAs (ensure_walkability_layers) -- override just the legend
    # tree node's display name so the on-page legend reads "Walkability"
    # like the other map types' clean reference-layer headings.
    lt_layer.setName("Walkability")
    legend.model().setRootGroup(root)
    _KEEP_ALIVE.append(root)
    legend.model().refreshLayerLegend(lt_layer)

    legend.setSymbolHeight(2.5)
    legend.setSymbolWidth(8)
    legend.setReferencePoint(QgsLayoutItem.ReferencePoint.LowerLeft)
    legend.attemptMove(QgsLayoutPoint(8, PAGE_HEIGHT_MM - 8, QgsUnitTypes.LayoutMillimeters))
    legend.attemptResize(QgsLayoutSize(45, 50, QgsUnitTypes.LayoutMillimeters))
    layout.addLayoutItem(legend)

    manager.addLayout(layout)
    return layout


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
    requested = list(MAP_TYPE_DEFS) if "all" in args.maps else list(dict.fromkeys(args.maps))
    want_walkability = "walkability" in requested
    selected = [k for k in requested if k in MAP_TYPE_DEFS]

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

    if args.all_streets:
        points = ensure_all_streets_points(project)
        if points is None:
            qgs.exitQgis()
            sys.exit(f"ERROR: could not load {ALL_STREETS_POINTS}")
        print(f"Loaded {points.featureCount():,} citywide streetview points")
        title_text = "All Streets — Public Realms"
        layout = build_walkability_layout(project, title_text, title_text, "All_Streets",
                                           None, points, context_layers)
        png_path = PUBLIC_REALM_OUTPUTS / "All_Streets" / "All_Streets_walkability_print_map.png"
        ok = export_png(layout, png_path)
        print(f"walkability: {'ok' if ok else 'EXPORT FAILED'} -> {png_path.name}", flush=True)
        if not project.write(str(PROJECT_PATH)):
            print("WARNING: failed to save project", file=sys.stderr)
        qgs.exitQgis()
        return

    resolved = {}
    if selected:
        summary_rows = load_summary_rows()
        resolved = resolve_map_types(project, selected, summary_rows)

    walkability_ranks, walkability_total = ({}, 0)
    if want_walkability:
        walkability_ranks, walkability_total = load_walkability_ranks()

    names = [args.bia] if args.bia else (LIMIT_TO if LIMIT_TO else get_bia_names())
    map_labels = selected + (["walkability"] if want_walkability else [])
    print(f"{len(names)} BIAs x {len(map_labels)} map type(s) "
          f"({', '.join(map_labels)}) = {len(names) * len(map_labels)} exports\n", flush=True)

    built, skipped, failed = 0, [], []
    for i, name in enumerate(names, 1):
        slug = slugify(name)

        if selected:
            lyr = ensure_layer(project, slug)
            if lyr is None:
                skipped.append(name)
                print(f"  [{i}/{len(names)}] SKIP (no gpkg): {name}", flush=True)
            else:
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

        if want_walkability:
            wlyrs = ensure_walkability_layers(project, slug)
            if wlyrs is None:
                skipped.append(f"{name} (walkability)")
                print(f"  [{i}/{len(names)}] SKIP (no streetview gpkg): {name}", flush=True)
            else:
                try:
                    rank = walkability_ranks.get(name)
                    rank_suffix = f"({rank}/{walkability_total})" if rank is not None else ""
                    title_text = f"{name} — Public Realms {rank_suffix}".strip()
                    layout = build_walkability_layout(project, f"{name} — Public Realms", title_text, slug,
                                                        wlyrs["network"], wlyrs["points"], context_layers)
                    png_path = PUBLIC_REALM_OUTPUTS / slug / f"{slug}_walkability_print_map.png"
                    ok = export_png(layout, png_path)
                    built += 1
                    print(f"  [{i}/{len(names)}] walkability: {'ok' if ok else 'EXPORT FAILED'} "
                          f"-> {png_path.name}", flush=True)
                except Exception as exc:
                    failed.append(f"{name} (walkability)")
                    print(f"  [{i}/{len(names)}] walkability: FAILED -- {exc}", flush=True)

        if i % 10 == 0:
            project.write(str(PROJECT_PATH))
            print("  (checkpoint saved)", flush=True)

    if not project.write(str(PROJECT_PATH)):
        sys.exit("ERROR: failed to save project")

    print(f"\nDone. {built} built, {len(skipped)} skipped, {len(failed)} failed.", flush=True)
    if skipped:
        print("Skipped:")
        for n in skipped:
            print(f"  - {n}")
    if failed:
        print("Failed (exception during build):")
        for n in failed:
            print(f"  - {n}")

    qgs.exitQgis()


if __name__ == "__main__":
    main()
