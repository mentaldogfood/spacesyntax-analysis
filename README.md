# Space Syntax Analysis Pipeline for Toronto BIAs

This pipeline runs spatial syntax analysis for road segments for these metrics: (Normalized Angular Integration--NAIN, Normalized Angular Choice--NACH, their Pearson R correlation, segment length) at the BIA boundary scale (with a 800m buffer to reduce edge effects)

Each of the 86 BIA's total NAIN/NACH is a weighted average of its own local segment analysis and the citywide segment analysis (70% local / 30% citywide by default), to account for movement and activity at different radii.

Scores are joined back to the road geometry and exported as scored GPKG/MIF, PNG, along with other key statistics which have been produced for the entire set of road segments for each BIA. Those can be found in the /outputs folder.

There is also a city-wide summary CSV which accumulates the summary statistics for all 86 BIAs, with each BIA ranked (1 = best) on blended NAIN, blended NACH, average segment length (lower ranks better here), and the blended NAIN-NACH Pearson r.

## Methodology

Key statistical terms and formulas are derived from these pieces of literature

https://www.mdpi.com/2220-9964/12/6/216
https://www.mdpi.com/2071-1050/13/6/3394
https://cyprusconferences.org/14sss/wp-content/uploads/2024/05/sss142024_4.pdf 

**NAIN (Normalized Angular Integration)** 

How well integrated a street is with the rest of the road network, usually high for streets with commercial hubs. Normalized to be comparable across different catchment area sizes.

=(value("T1024 Node Count")^(1.2))/(value("T1024 Total Depth")+2)

**NACH (Normalized Angular Choice)**

How often is a street chosen in the commute path between two points within the system, usually high for street with heavy commute traffic. Normalized to be comparable across different catchment area sizes.

=log(value("T1024 Choice")+1)/log(value("T1024 Total Depth")+3)

**Local / Citywide Weighted Average**

Local (per-BIA) and citywide segment analyses are two independent depthmapX runs — different segmentations, no shared segment ID — so each local segment is matched to its nearest citywide segment by geometry

local_nain, local_nach   — this BIA's own segment analysis
citywide_nain, citywide_nach  — the matched segment's citywide-scale analysis
blended_nain = local_weight × local_nain + (1 − local_weight) × citywide_nain
blended_nach = local_weight × local_nach + (1 − local_weight) × citywide_nach

Default `local_weight` = 0.7 (70% local / 30% citywide).

**INCH (Correlation, Pearson R)**

Measure accessibility, or how closely aligned are the well-integrated streets with the high commuter-traffic streets.

=corr(NAIN, NACH)

## Requirements

```
pip install -r requirements.txt
```

The DepthmapX CLI, developed by Space Syntax at UCL, is already attached within the repo. However you could find them here:

DepthmapX CLI download: https://github.com/SpaceGroupUCL/depthmapX/releases

## Usage

1. extract_bia: clip road centrelines to buffered BIA boundaries, then export as DXF

2. depthmapx_segment: run depthmapXcli angular segment analysis, compute derived metrics, export .graph/MIF/GPKG/CSV

3. citywide_match: match each local segment to its citywide counterpart (which I pregenerated manually using the depthmapX GUI, as the file citywide_Segment_Map.csv). 
                          
the spatial join was performed based on proximity, within a distance tolerance. the local + citywide NAIN/NACH is also weighted in this step.

4. visualization: aggregate blended NACH and NAIN values, computes their Pearson R correlation, ranks each BIA for each metric, then exports to PyQGIS for visuals. 

5. scoring_aggregation: interpolates GSV streetview walkability point scores (pulled from the public-realm-ai project's per-BIA streetview_analysis.geojson) onto this repo's own road centerlines via IDW, so walkability can be mapped on the same network geometry as NAIN/NACH.


Running full analysis on one BIA:
```
python main.py --bia "Downtown Yonge"
```
Checking list of BIA names:
```
python main.py --list-bias
```
Running full analysis on all BIAs:
```
python main.py --all-bias
```
Adjusting weight on the local integration score (citywide gets 1 - this)
```
--local-weight 0.7          
```
Or if you only want the dxfs, after buffering and clipping:
```
python modules/extract_bia_boundaries.py --bia "Bloor West Village"
```
To build print-quality QGIS report layouts (must run with QGIS's own Python, not the pipeline's venv):
```
& "C:\Program Files\QGIS 3.44.7\bin\python-qgis.bat" modules\visualization.py
& "C:\Program Files\QGIS 3.44.7\bin\python-qgis.bat" modules\visualization.py --maps all
& "C:\Program Files\QGIS 3.44.7\bin\python-qgis.bat" modules\visualization.py --maps choice
& "C:\Program Files\QGIS 3.44.7\bin\python-qgis.bat" modules\visualization.py --maps segment_length
```
Before the walkability maps can be built, interpolate the streetview scores onto this repo's centerlines (also needs QGIS's own Python):
```
& "C:\Program Files\QGIS 3.44.7\bin\python-qgis.bat" modules\scoring_aggregation.py --bia "Downtown Yonge"
& "C:\Program Files\QGIS 3.44.7\bin\python-qgis.bat" modules\scoring_aggregation.py --all-bias
```
Then export the walkability maps (opt-in, not part of `--maps all`):
```
& "C:\Program Files\QGIS 3.44.7\bin\python-qgis.bat" modules\visualization.py --maps walkability
```