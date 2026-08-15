"""
extract_bia.py — clip road centrelines to a BIA + buffer and export as DXF.

Usage:
    python extract_bia.py --bia "Downtown Yonge"
    python extract_bia.py --list-bias
    python extract_bia.py --bia "Downtown Yonge" --buffer 1200

Importable:
    from extract_bia import extract_bia
    dxf_path, clipped, bia_gdf = extract_bia("Downtown Yonge", output_dir)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import ezdxf
import geopandas as gpd

TORONTO_CRS  = "EPSG:26917"
BASE         = Path(__file__).parent.parent   # scripts/ -> project root
INPUTS       = BASE / "inputs"
ROADS_FILE   = INPUTS / "cleaned up centerlines.gpkg"
BIAS_FILE    = INPUTS / "all bia boundaries.geojson"
OUTPUT_ROOT  = BASE / "outputs"
BIA_BUFFER_M = 1000.0


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def get_bia_names() -> list[str]:
    bias = gpd.read_file(BIAS_FILE)
    return sorted(bias["AREA_NAME"].dropna().tolist())


def list_bias() -> None:
    print("Available BIAs:")
    for name in get_bia_names():
        print(f"  {name}")


def load_bia(name: str) -> gpd.GeoDataFrame:
    bias = gpd.read_file(BIAS_FILE)
    match = bias[bias["AREA_NAME"].str.strip().str.lower() == name.strip().lower()]
    if match.empty:
        list_bias()
        sys.exit(f"\nERROR: BIA '{name}' not found.")
    return match.to_crs(TORONTO_CRS)


def load_roads() -> gpd.GeoDataFrame:
    return gpd.read_file(ROADS_FILE, layer="cleaned_up_centerlines").to_crs(TORONTO_CRS)


# ---------------------------------------------------------------------------
# Clipping
# ---------------------------------------------------------------------------

def clip_roads(
    roads: gpd.GeoDataFrame,
    bia:   gpd.GeoDataFrame,
    buffer_m: float = BIA_BUFFER_M,
) -> gpd.GeoDataFrame:
    poly = bia.geometry.union_all().buffer(buffer_m)
    return gpd.clip(roads, poly).reset_index(drop=True)


def explode_linestrings(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)
    mask = (
        gdf.geometry.notna()
        & ~gdf.geometry.is_empty
        & (gdf.geometry.geom_type == "LineString")
    )
    return gdf[mask].reset_index(drop=True)


# ---------------------------------------------------------------------------
# DXF export
# ---------------------------------------------------------------------------

def write_dxf(gdf: gpd.GeoDataFrame, path: Path) -> None:
    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    count = 0
    for geom in gdf.geometry:
        # Force 2D — depthmapXnet cannot handle Z coordinates
        pts = [(float(x), float(y)) for x, y, *_ in geom.coords]
        if len(pts) >= 2:
            msp.add_lwpolyline(pts, dxfattribs={"layer": "ROADS"})
            count += 1
    doc.saveas(str(path))
    print(f"    DXF written: {path.name}  ({count:,} polylines)")


# ---------------------------------------------------------------------------
# Main extraction function (importable)
# ---------------------------------------------------------------------------

def extract_citywide(output_dir: Path) -> tuple[Path, gpd.GeoDataFrame]:
    """
    Export the full, unclipped road network as a DXF (same export format as
    extract_bia, minus the BIA boundary clip).

    Returns
    -------
    dxf_path : Path to the exported Citywide_centerlines.dxf
    roads    : GeoDataFrame of exploded road segments (UTM)
    """
    slug = "Citywide"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Extracting citywide network (no clipping)")
    print(f"  Output         : {output_dir}")
    print(f"{'='*60}\n")

    print("Loading data...")
    roads = load_roads()
    print(f"  {len(roads):,} road features loaded")

    print("\nExploding to individual linestrings...")
    exploded = explode_linestrings(roads)
    print(f"  {len(exploded):,} segments after explode")

    dxf_path = output_dir / f"{slug}_centerlines.dxf"
    write_dxf(exploded, dxf_path)

    gpkg_path = output_dir / f"{slug}_centerlines.gpkg"
    if gpkg_path.exists():
        try:
            gpkg_path.unlink()
        except PermissionError:
            pass  # file locked (e.g. open in QGIS); overwrite in place
    exploded.to_file(gpkg_path, driver="GPKG")
    print(f"    GeoPackage   : {gpkg_path.name}")

    return dxf_path, exploded


def extract_bia(
    bia_name:   str,
    output_dir: Path,
    buffer_m:   float = BIA_BUFFER_M,
) -> tuple[Path, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    Clip road centrelines to the named BIA (plus a buffer) and export as DXF.

    Returns
    -------
    dxf_path   : Path to the exported *_centerlines.dxf
    clipped    : GeoDataFrame of clipped road segments (UTM)
    bia_gdf    : GeoDataFrame of the BIA boundary (UTM, used for SVG rendering)
    """
    slug = bia_name.strip().replace(" ", "_").replace("/", "-")
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Extracting BIA : {bia_name}")
    print(f"  Buffer         : {buffer_m:.0f} m")
    print(f"  Output         : {output_dir}")
    print(f"{'='*60}\n")

    print("Loading data...")
    bia   = load_bia(bia_name)
    roads = load_roads()
    print(f"  {len(roads):,} road features loaded")

    print(f"\nClipping to BIA + {buffer_m:.0f} m buffer...")
    clipped = explode_linestrings(clip_roads(roads, bia, buffer_m))
    print(f"  {len(clipped):,} segments after clip + explode")

    dxf_path = output_dir / f"{slug}_centerlines.dxf"
    write_dxf(clipped, dxf_path)

    gpkg_path = output_dir / f"{slug}_centerlines.gpkg"
    if gpkg_path.exists():
        try:
            gpkg_path.unlink()
        except PermissionError:
            pass  # file locked (e.g. open in QGIS); overwrite in place
    clipped.to_file(gpkg_path, driver="GPKG")
    print(f"    GeoPackage   : {gpkg_path.name}")

    return dxf_path, clipped, bia


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Clip road centrelines to a BIA and export as DXF",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--bia",       help="BIA name, e.g. 'Downtown Yonge'")
    g.add_argument("--list-bias", action="store_true", help="List available BIAs and exit")
    p.add_argument("--buffer",    type=float, default=BIA_BUFFER_M,
                   help="Buffer distance around BIA boundary (metres)")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Override output directory")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.list_bias:
        list_bias()
        sys.exit(0)

    slug    = args.bia.strip().replace(" ", "_").replace("/", "-")
    out_dir = args.output_dir or (OUTPUT_ROOT / slug)
    dxf, _, _ = extract_bia(args.bia, out_dir, buffer_m=args.buffer)
    print(f"\nDone.  DXF: {dxf}")


if __name__ == "__main__":
    main()
