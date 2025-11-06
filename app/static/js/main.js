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