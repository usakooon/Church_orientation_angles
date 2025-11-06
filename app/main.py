import asyncio
import csv
import io
import json
from typing import Any, Dict, List, Optional, Tuple

import httpx
import osmnx as ox
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from pyproj import Geod

try:
    from shapely.validation import make_valid
except ImportError:  # pragma: no cover - fallback for older Shapely
    def make_valid(geom):  # type: ignore
        return geom.buffer(0)

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping
from shapely.ops import unary_union


class BBox(BaseModel):
    north: float
    south: float
    east: float
    west: float


class ChurchRequest(BaseModel):
    bbox: BBox


class ChurchFeature(BaseModel):
    name: Optional[str]
    lat: float
    lon: float
    orientation_deg: float
    deviation_deg: float


app = FastAPI(title="Church Orientation Explorer")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

USER_AGENT = "ChurchOrientationExplorer/1.0"
GEOD = Geod(ellps="WGS84")


def _normalize_geometry(geom: Any) -> Optional[Polygon]:
    if geom is None:
        return None
    geom = make_valid(geom)
    if geom.is_empty:
        return None
    if isinstance(geom, GeometryCollection):
        parts = [part for part in geom.geoms if part.geom_type in {"Polygon", "MultiPolygon"}]
        if not parts:
            return None
        geom = unary_union(parts)
    if isinstance(geom, MultiPolygon):
        polygons = [poly for poly in geom.geoms if not poly.is_empty]
        if not polygons:
            return None
        geom = max(polygons, key=lambda p: p.area)
    if not isinstance(geom, Polygon):
        return None
    return geom


def _angular_difference(a: float, b: float) -> float:
    diff = abs(a - b) % 360.0
    return diff if diff <= 180.0 else 360.0 - diff


def _calculate_metrics(geom: Polygon) -> Dict[str, Any]:
    rect = geom.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)
    if len(coords) < 2:
        raise ValueError("Not enough coordinates to determine orientation")

    longest_length = 0.0
    orientation = 0.0
    for idx in range(len(coords) - 1):
        lon1, lat1 = coords[idx]
        lon2, lat2 = coords[idx + 1]
        if lon1 == lon2 and lat1 == lat2:
            continue
        az12, _, dist = GEOD.inv(lon1, lat1, lon2, lat2)
        if dist > longest_length:
            longest_length = dist
            orientation = (az12 + 360.0) % 360.0
    if longest_length == 0.0:
        raise ValueError("Unable to determine longest edge")

    centroid = geom.representative_point()
    deviation = min(
        _angular_difference(orientation, 90.0),
        _angular_difference(orientation, 270.0),
    )

    arrow_length = max(20.0, min(longest_length * 0.5, 80.0))
    arrow_lon, arrow_lat, _ = GEOD.fwd(centroid.x, centroid.y, orientation, arrow_length)

    return {
        "center_lat": centroid.y,
        "center_lon": centroid.x,
        "orientation_deg": orientation,
        "deviation_deg": deviation,
        "long_edge_m": longest_length,
        "arrow_end_lat": arrow_lat,
        "arrow_end_lon": arrow_lon,
    }


def _geometry_to_feature(
    geom: Polygon, properties: Dict[str, Any]
) -> Dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": mapping(geom),
        "properties": properties,
    }


def _arrow_feature(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [data["center_lon"], data["center_lat"]],
                [data["arrow_end_lon"], data["arrow_end_lat"]],
            ],
        },
        "properties": {
            "orientation_deg": data["orientation_deg"],
        },
    }


async def _query_churches(bbox: BBox) -> Tuple[List[ChurchFeature], Dict[str, Any], Dict[str, Any]]:
    def worker() -> Tuple[List[ChurchFeature], Dict[str, Any], Dict[str, Any]]:
        gdf = ox.geometries_from_bbox(
            bbox.north,
            bbox.south,
            bbox.east,
            bbox.west,
            tags={"building": ["church", "cathedral"]},
        )

        if gdf.empty:
            return [], {"type": "FeatureCollection", "features": []}, {
                "type": "FeatureCollection",
                "features": [],
            }

        gdf = gdf.reset_index()

        features: List[ChurchFeature] = []
        polygon_features: List[Dict[str, Any]] = []
        arrow_features: List[Dict[str, Any]] = []

        for _, row in gdf.iterrows():
            geom = _normalize_geometry(row.geometry)
            if geom is None or geom.is_empty:
                continue
            try:
                metrics = _calculate_metrics(geom)
            except ValueError:
                continue

            name = row.get("name") if isinstance(row.get("name"), str) else None
            feature = ChurchFeature(
                name=name,
                lat=metrics["center_lat"],
                lon=metrics["center_lon"],
                orientation_deg=metrics["orientation_deg"],
                deviation_deg=metrics["deviation_deg"],
            )
            features.append(feature)

            properties = {
                "name": name,
                "orientation_deg": metrics["orientation_deg"],
                "deviation_deg": metrics["deviation_deg"],
                "long_edge_m": metrics["long_edge_m"],
            }
            polygon_features.append(_geometry_to_feature(geom, properties))
            arrow_features.append(_arrow_feature(metrics))

        return (
            features,
            {"type": "FeatureCollection", "features": polygon_features},
            {"type": "FeatureCollection", "features": arrow_features},
        )

    return await asyncio.to_thread(worker)


@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/geocode")
async def geocode(query: str) -> Dict[str, Any]:
    params = {"q": query, "format": "json", "limit": 1}
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params=params,
            headers=headers,
        )
    response.raise_for_status()
    data = response.json()
    if not data:
        raise HTTPException(status_code=404, detail="指定した都市は見つかりませんでした。")

    item = data[0]
    bounding = item.get("boundingbox")
    if not bounding or len(bounding) != 4:
        raise HTTPException(status_code=500, detail="ジオコーディング結果に境界がありません。")

    south, north, west, east = map(float, bounding)
    return {
        "lat": float(item["lat"]),
        "lon": float(item["lon"]),
        "bbox": {
            "north": north,
            "south": south,
            "east": east,
            "west": west,
        },
    }


@app.post("/api/churches")
async def churches(request: ChurchRequest) -> JSONResponse:
    features, polygons, arrows = await _query_churches(request.bbox)
    return JSONResponse(
        {
            "features": [feature.dict() for feature in features],
            "polygons": polygons,
            "arrows": arrows,
        }
    )


@app.post("/api/churches/export")
async def export_churches(
    request: ChurchRequest, format: str = Query("csv", regex="^(csv|geojson)$")
) -> StreamingResponse:
    features, polygons, _ = await _query_churches(request.bbox)

    if format == "geojson":
        geojson = json.dumps(polygons)
        return StreamingResponse(
            io.StringIO(geojson),
            media_type="application/geo+json",
            headers={
                "Content-Disposition": 'attachment; filename="church_orientations.geojson"'
            },
        )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "lat", "lon", "orientation_deg", "deviation_deg"])
    for feature in features:
        writer.writerow(
            [
                feature.name or "",
                f"{feature.lat:.6f}",
                f"{feature.lon:.6f}",
                f"{feature.orientation_deg:.2f}",
                f"{feature.deviation_deg:.2f}",
            ]
        )

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="church_orientations.csv"'
        },
    )
