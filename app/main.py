import asyncio
import csv
import io
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry import mapping as geom_mapping
from shapely.ops import transform, unary_union

try:  # pragma: no cover - Shapely 1.x fallback
    from shapely.validation import make_valid
except ImportError:  # pragma: no cover - older Shapely
    def make_valid(geometry):
        return geometry.buffer(0)


EARTH_RADIUS_M = 6_378_137.0
USER_AGENT = "ChurchOrientationExplorer/1.0 (+https://example.com/contact)"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
BASE_DIR = Path(__file__).resolve().parent


class BBox(BaseModel):
    north: float = Field(..., description="Northern latitude of the bbox")
    south: float = Field(..., description="Southern latitude of the bbox")
    east: float = Field(..., description="Eastern longitude of the bbox")
    west: float = Field(..., description="Western longitude of the bbox")

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return self.south, self.west, self.north, self.east


class OrientationRequest(BaseModel):
    bbox: BBox


app = FastAPI(title="Church Orientation Explorer")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.state.last_result: Dict[str, Any] = {"features": [], "geojson": None}


def _coords_to_polygon(coords: Iterable[Dict[str, float]]) -> Optional[Polygon]:
    points = [(pt["lon"], pt["lat"]) for pt in coords]
    if len(points) < 4:
        return None
    if points[0] != points[-1]:
        points.append(points[0])
    polygon = Polygon(points)
    polygon = make_valid(polygon)
    if polygon.is_empty:
        return None
    if isinstance(polygon, GeometryCollection):
        polygons = [geom for geom in polygon.geoms if isinstance(geom, Polygon)]
        if not polygons:
            return None
        polygon = unary_union(polygons)
    if isinstance(polygon, MultiPolygon):
        polygons = [geom for geom in polygon.geoms if not geom.is_empty]
        if not polygons:
            return None
        polygon = max(polygons, key=lambda geom: geom.area)
    if not isinstance(polygon, Polygon) or polygon.is_empty:
        return None
    return polygon


def _element_to_geometry(element: Dict[str, Any]) -> Optional[Any]:
    if element.get("type") == "way":
        geometry = element.get("geometry", [])
        return _coords_to_polygon(geometry)

    if element.get("type") == "relation":
        members = element.get("members", [])
        outers: List[Polygon] = []
        inners: List[Polygon] = []
        for member in members:
            coords = member.get("geometry")
            if not coords:
                continue
            polygon = _coords_to_polygon(coords)
            if polygon is None:
                continue
            if member.get("role") == "inner":
                inners.append(polygon)
            else:
                outers.append(polygon)

        if not outers and element.get("geometry"):
            polygon = _coords_to_polygon(element.get("geometry"))
            if polygon is not None:
                outers.append(polygon)

        if not outers:
            if inners:
                merged_inners = unary_union(inners)
                return merged_inners if not merged_inners.is_empty else None
            return None

        inner_union = unary_union(inners) if inners else None
        prepared: List[Any] = []
        for outer in outers:
            geom = make_valid(outer)
            if inner_union and not inner_union.is_empty:
                geom = geom.difference(inner_union)
            if geom.is_empty:
                continue
            prepared.append(geom)

        if not prepared:
            return None

        combined = unary_union(prepared)
        return combined if not combined.is_empty else None

    return None


def _extract_main_polygon(geometry: Any) -> Optional[Polygon]:
    if isinstance(geometry, Polygon):
        return geometry
    if isinstance(geometry, MultiPolygon):
        polygons = [geom for geom in geometry.geoms if isinstance(geom, Polygon) and not geom.is_empty]
        if not polygons:
            return None
        return max(polygons, key=lambda geom: geom.area)
    if isinstance(geometry, GeometryCollection):
        polygons = [
            _extract_main_polygon(part)
            for part in geometry.geoms
        ]
        polygons = [poly for poly in polygons if poly is not None]
        if not polygons:
            return None
        return max(polygons, key=lambda geom: geom.area)
    return None


def _project_geometry(polygon: Polygon) -> Tuple[Any, float]:
    reference_lat = polygon.representative_point().y
    cos_ref = math.cos(math.radians(reference_lat)) or 1e-9

    def _project(lon: float, lat: float, _: Optional[float] = None) -> Tuple[float, float]:
        x = math.radians(lon) * EARTH_RADIUS_M * cos_ref
        y = math.radians(lat) * EARTH_RADIUS_M
        return x, y

    projected = transform(_project, polygon)
    return projected, reference_lat


def _dedupe_lengths(lengths: List[float]) -> List[float]:
    unique: List[float] = []
    for length in sorted(lengths, reverse=True):
        if not unique or not math.isclose(length, unique[-1], rel_tol=1e-6, abs_tol=1e-6):
            unique.append(length)
    return unique


def _bearing_orientation(dx: float, dy: float) -> float:
    angle = math.degrees(math.atan2(dx, dy))
    return (angle + 360.0) % 360.0


def _deviation(angle: float, reference: float) -> float:
    diff = abs((angle - reference) % 360.0)
    if diff > 180.0:
        diff = 360.0 - diff
    return diff


def _calculate_metrics(geometry: Any) -> Dict[str, Any]:
    polygon = _extract_main_polygon(geometry)
    if polygon is None or polygon.is_empty:
        raise ValueError("No polygon geometry available for metrics calculation")

    projected, reference_lat = _project_geometry(polygon)
    rectangle = projected.minimum_rotated_rectangle
    coords = list(rectangle.exterior.coords)
    if len(coords) < 5:
        raise ValueError("Unable to derive oriented bounding box")

    edge_lengths: List[float] = []
    orientation = 0.0
    longest = 0.0
    for idx in range(4):
        x1, y1 = coords[idx]
        x2, y2 = coords[idx + 1]
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length <= 0:
            continue
        edge_lengths.append(length)
        if length > longest:
            longest = length
            orientation = _bearing_orientation(dx, dy)

    if longest == 0.0 or not edge_lengths:
        raise ValueError("Degenerate bounding rectangle")

    unique_lengths = _dedupe_lengths(edge_lengths)
    long_side = unique_lengths[0]
    short_side = unique_lengths[1] if len(unique_lengths) > 1 else unique_lengths[0]
    aspect_ratio = long_side / short_side if short_side > 0 else 0.0

    orientation_deg = orientation % 360.0
    deviation_deg = min(_deviation(orientation_deg, 90.0), _deviation(orientation_deg, 270.0))
    signed_dev_deg = (orientation_deg % 180.0) - 90.0
    confidence = "high" if aspect_ratio >= 1.2 else "low"

    center = polygon.representative_point()
    center_lon = center.x
    center_lat = center.y

    arrow_length = max(min(long_side * 0.5, 150.0), 30.0)
    orientation_rad = math.radians(orientation_deg)
    dx = arrow_length * math.sin(orientation_rad)
    dy = arrow_length * math.cos(orientation_rad)
    cos_ref = math.cos(math.radians(center_lat)) or 1e-9
    delta_lon = math.degrees(dx / (EARTH_RADIUS_M * cos_ref))
    delta_lat = math.degrees(dy / EARTH_RADIUS_M)
    arrow_lon = center_lon + delta_lon
    arrow_lat = center_lat + delta_lat

    return {
        "orientation_deg": orientation_deg,
        "deviation_deg": deviation_deg,
        "signed_dev_deg": signed_dev_deg,
        "aspect_ratio": aspect_ratio,
        "confidence": confidence,
        "center_lon": center_lon,
        "center_lat": center_lat,
        "arrow_lon": arrow_lon,
        "arrow_lat": arrow_lat,
    }


def _build_query(bbox: BBox) -> str:
    south, west, north, east = bbox.as_tuple()
    return (
        "[out:json][timeout:25];"
        "("
        f"way[\"building\"~\"^(church|cathedral)$\"]({south},{west},{north},{east});"
        f"relation[\"building\"~\"^(church|cathedral)$\"]({south},{west},{north},{east});"
        ");"
        "out geom;"
    )


def _process_elements(elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    features: List[Dict[str, Any]] = []
    geo_features: List[Dict[str, Any]] = []

    for element in elements:
        geometry = _element_to_geometry(element)
        if geometry is None or geometry.is_empty:
            continue
        geometry = make_valid(geometry)
        if geometry.is_empty:
            continue
        try:
            metrics = _calculate_metrics(geometry)
        except ValueError:
            continue

        tags = element.get("tags", {})
        name = tags.get("name") if isinstance(tags.get("name"), str) else None

        feature_entry = {
            "name": name,
            "lat": metrics["center_lat"],
            "lon": metrics["center_lon"],
            "orientation_deg": metrics["orientation_deg"],
            "deviation_deg": metrics["deviation_deg"],
            "signed_dev_deg": metrics["signed_dev_deg"],
            "aspect_ratio": metrics["aspect_ratio"],
            "confidence": metrics["confidence"],
            "arrow_lat": metrics["arrow_lat"],
            "arrow_lon": metrics["arrow_lon"],
        }
        features.append(feature_entry)

        properties = {
            **feature_entry,
            "osm_id": f"{element.get('type')}/{element.get('id')}",
        }
        geo_features.append({
            "type": "Feature",
            "geometry": geom_mapping(geometry),
            "properties": properties,
        })

    return {
        "features": features,
        "geojson": {"type": "FeatureCollection", "features": geo_features},
    }


async def _fetch_orientation(bbox: BBox) -> Dict[str, Any]:
    query = _build_query(bbox)

    def _request() -> Dict[str, Any]:
        try:
            response = requests.post(
                OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=90,
            )
            response.raise_for_status()
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise HTTPException(status_code=502, detail="Overpass API request failed") from exc
        data = response.json()
        elements = data.get("elements", [])
        return _process_elements(elements)

    return await asyncio.to_thread(_request)


async def _geocode_city(query: str) -> Dict[str, Any]:
    params = {"q": query, "format": "json", "limit": 1}

    async def _throttled_request() -> Dict[str, Any]:
        await asyncio.sleep(1.0)

        def _request() -> Dict[str, Any]:
            try:
                response = requests.get(
                    NOMINATIM_URL,
                    params=params,
                    headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
                    timeout=30,
                )
                response.raise_for_status()
            except requests.RequestException as exc:  # pragma: no cover - network failure
                raise HTTPException(status_code=502, detail="Nominatim request failed") from exc
            data = response.json()
            if not data:
                raise HTTPException(status_code=404, detail="City not found")
            item = data[0]
            bounding = item.get("boundingbox")
            if not bounding or len(bounding) != 4:
                raise HTTPException(status_code=500, detail="Geocoding result is missing a bounding box")
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

        return await asyncio.to_thread(_request)

    return await _throttled_request()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/search_city")
async def search_city(query: str) -> Dict[str, Any]:
    return await _geocode_city(query)


@app.post("/api/orientation")
async def orientation(request: OrientationRequest) -> JSONResponse:
    result = await _fetch_orientation(request.bbox)
    app.state.last_result = result
    return JSONResponse(result)


@app.get("/api/export.csv")
async def export_csv() -> StreamingResponse:
    last_result = app.state.last_result
    features: List[Dict[str, Any]] = last_result.get("features", []) if isinstance(last_result, dict) else []
    if not features:
        raise HTTPException(status_code=404, detail="No results available. Run a search first.")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "name",
            "lat",
            "lon",
            "orientation_deg",
            "deviation_deg",
            "signed_dev_deg",
            "aspect_ratio",
            "confidence",
        ]
    )
    for feature in features:
        writer.writerow(
            [
                feature.get("name", "") or "",
                f"{feature['lat']:.6f}",
                f"{feature['lon']:.6f}",
                f"{feature['orientation_deg']:.2f}",
                f"{feature['deviation_deg']:.2f}",
                f"{feature['signed_dev_deg']:.2f}",
                f"{feature['aspect_ratio']:.2f}",
                feature.get("confidence", ""),
            ]
        )

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="church_orientation.csv"'},
    )


@app.get("/api/export.geojson")
async def export_geojson() -> JSONResponse:
    last_result = app.state.last_result
    geojson = last_result.get("geojson") if isinstance(last_result, dict) else None
    if not geojson or not geojson.get("features"):
        raise HTTPException(status_code=404, detail="No results available. Run a search first.")
    return JSONResponse(geojson)


if __name__ == "__main__":  # pragma: no cover - CLI usage
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
app/static/css/styles.css
新規
+153
-0

body {
  height: 100%;
  margin: 0;
  font-family: "Noto Sans JP", system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", sans-serif;
  color: #1f2933;
  background-color: #f8fafc;
}

.app-container {
  display: flex;
  height: 100vh;
  width: 100vw;
  overflow: hidden;
}

.map-panel {
  flex: 2;
  display: flex;
  flex-direction: column;
  border-right: 1px solid #d9e2ec;
  background-color: #ffffff;
}

.controls {
  padding: 16px;
  border-bottom: 1px solid #d9e2ec;
  background-color: #f0f4f8;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.controls .button-row,
.export-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.controls button,
.export-buttons button {
  padding: 8px 14px;
  border: none;
  background-color: #2563eb;
  color: #ffffff;
  border-radius: 4px;
  cursor: pointer;
  transition: background-color 0.2s ease-in-out;
}

.controls button:hover,
.export-buttons button:hover {
  background-color: #1d4ed8;
}

.controls button:disabled,
.export-buttons button:disabled {
  background-color: #94a3b8;
  cursor: not-allowed;
}

#status {
  font-size: 0.85rem;
  color: #334e68;
  min-height: 1.2em;
}

#status.info {
  color: #334e68;
}

#status.success {
  color: #047857;
}

#status.error {
  color: #b91c1c;
}

#status.loading {
  color: #7c3aed;
}

#map {
  flex: 1;
  min-height: 0;
}

.table-panel {
  flex: 1;
  padding: 16px;
  overflow: auto;
  background-color: #ffffff;
}

.table-panel h1 {
  margin-top: 0;
  margin-bottom: 12px;
  font-size: 1.4rem;
  color: #1f2933;
}

#results-table {
  width: 100%;
  border-collapse: collapse;
}

#results-table th,
#results-table td {
  border-bottom: 1px solid #d9e2ec;
  padding: 8px 6px;
  text-align: left;
  font-size: 0.9rem;
}

#results-table tbody tr:nth-child(odd) {
  background-color: #f8fafc;
}

.arrow-icon-wrapper {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}

.arrow-icon {
  font-size: 18px;
  color: #c0392b;
  transform-origin: center center;
}

.leaflet-container {
  font: inherit;
}

@media (max-width: 960px) {
  .app-container {
    flex-direction: column;
  }

  .map-panel {
    flex: none;
    height: 55vh;
  }

  .table-panel {
    height: 45vh;
  }
}
app/static/js/main.js
新規
+236
-0

const map = L.map("map", { zoomControl: true }).setView([45.4642, 9.1919], 13);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/">OpenStreetMap</a> contributors',
  maxZoom: 19,
}).addTo(map);

const polygonLayer = L.geoJSON(null, {
  style: () => ({
    color: "#2563eb",
    weight: 1.2,
    fillColor: "#60a5fa",
    fillOpacity: 0.3,
  }),
}).addTo(map);

const arrowLayer = L.layerGroup().addTo(map);

const statusEl = document.getElementById("status");
const searchCityButton = document.getElementById("search-city-btn");
const searchBboxButton = document.getElementById("search-bbox-btn");
const exportCsvButton = document.getElementById("export-csv-btn");
const exportGeoJsonButton = document.getElementById("export-geojson-btn");
const tableBody = document.querySelector("#results-table tbody");

let hasResults = false;

function setStatus(message, type = "info") {
  if (!message) {
    statusEl.textContent = "";
    statusEl.className = "";
    return;
  }
  statusEl.textContent = message;
  statusEl.className = type;
}

function formatNumber(value, digits = 2) {
  return Number.parseFloat(value).toFixed(digits);
}

function createArrowIcon(angle) {
  return L.divIcon({
    html: `<div class="arrow-icon" style="transform: rotate(${angle}deg)">↑</div>`,
    className: "arrow-icon-wrapper",
    iconSize: [24, 24],
    iconAnchor: [12, 12],
  });
}

function clearLayers() {
  polygonLayer.clearLayers();
  arrowLayer.clearLayers();
}

function updateTable(features) {
  tableBody.innerHTML = "";
  if (!features || !features.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.textContent = "No buildings found.";
    row.appendChild(cell);
    tableBody.appendChild(row);
    return;
  }

  for (const feature of features) {
    const row = document.createElement("tr");
    const name = feature.name || "(unnamed)";
    row.innerHTML = `
      <td>${name}</td>
      <td>${formatNumber(feature.lat, 6)}</td>
      <td>${formatNumber(feature.lon, 6)}</td>
      <td>${formatNumber(feature.orientation_deg, 1)}</td>
      <td>${formatNumber(feature.deviation_deg, 1)}</td>
      <td>${formatNumber(feature.signed_dev_deg, 1)}</td>
      <td>${formatNumber(feature.aspect_ratio, 2)}</td>
      <td>${feature.confidence}</td>
    `;
    tableBody.appendChild(row);
  }
}

function updateMap(geojson, features) {
  clearLayers();
  if (geojson?.features?.length) {
    polygonLayer.addData(geojson);
  }
  if (features?.length) {
    for (const feature of features) {
      const marker = L.marker([feature.lat, feature.lon], {
        icon: createArrowIcon(feature.orientation_deg),
        interactive: false,
      });
      arrowLayer.addLayer(marker);
      if (
        Number.isFinite(feature.arrow_lat) &&
        Number.isFinite(feature.arrow_lon)
      ) {
        const line = L.polyline(
          [
            [feature.lat, feature.lon],
            [feature.arrow_lat, feature.arrow_lon],
          ],
          {
            color: "#d97706",
            weight: 2,
            opacity: 0.9,
            interactive: false,
          }
        );
        arrowLayer.addLayer(line);
      }
    }
  }
}

async function requestOrientation(bbox) {
  setStatus("Fetching building orientations…", "loading");
  hasResults = false;

  try {
    const response = await fetch("/api/orientation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bbox }),
    });

    if (!response.ok) {
      const message = await response.text();
      throw new Error(message || `Server error: ${response.status}`);
    }

    const data = await response.json();
    const features = data.features || [];
    updateMap(data.geojson, features);
    updateTable(features);
    hasResults = features.length > 0;

    if (hasResults) {
      setStatus(`${features.length} building(s) found.`, "success");
    } else {
      setStatus("No churches or cathedrals found in this area.", "info");
    }
  } catch (error) {
    console.error(error);
    clearLayers();
    updateTable([]);
    setStatus("Failed to fetch orientation data.", "error");
    window.alert("Unable to fetch building data. Please try again later.");
  }
}

function getCurrentBbox() {
  const bounds = map.getBounds();
  return {
    north: bounds.getNorth(),
    south: bounds.getSouth(),
    east: bounds.getEast(),
    west: bounds.getWest(),
  };
}

searchCityButton.addEventListener("click", async () => {
  const query = window.prompt("Enter a city or place name (e.g., Milano)");
  if (!query) {
    return;
  }

  setStatus("Searching city via Nominatim…", "loading");

  try {
    const response = await fetch(
      `/api/search_city?query=${encodeURIComponent(query.trim())}`
    );
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || "City lookup failed");
    }
    const data = await response.json();
    const bbox = data.bbox;
    const bounds = L.latLngBounds(
      [bbox.south, bbox.west],
      [bbox.north, bbox.east]
    );
    map.fitBounds(bounds, { padding: [24, 24] });
    await requestOrientation(bbox);
  } catch (error) {
    console.error(error);
    setStatus("City search failed.", "error");
    window.alert("Unable to find that city. Please try another search term.");
  }
});

searchBboxButton.addEventListener("click", () => {
  const bbox = getCurrentBbox();
  requestOrientation(bbox);
});

async function exportData(endpoint) {
  if (!hasResults) {
    window.alert("Run a search before exporting.");
    return;
  }

  try {
    const response = await fetch(endpoint);
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || "Export failed");
    }
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = endpoint.endsWith("csv")
      ? "church_orientation.csv"
      : "church_orientation.geojson";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(url);
    setStatus("Export ready.", "success");
  } catch (error) {
    console.error(error);
    setStatus("Export failed.", "error");
    window.alert("Export failed. Please run a search and try again.");
  }
}

exportCsvButton.addEventListener("click", () => exportData("/api/export.csv"));
exportGeoJsonButton.addEventListener("click", () => exportData("/api/export.geojson"));

setStatus("Search for a city or use the current map view to begin.", "info");
