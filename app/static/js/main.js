const map = L.map("map", {
  zoomControl: true,
}).setView([45.4642, 9.1919], 13);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/">OpenStreetMap</a> contributors',
  maxZoom: 19,
}).addTo(map);

const drawnItems = new L.FeatureGroup();
map.addLayer(drawnItems);

const drawControl = new L.Control.Draw({
  draw: {
    polygon: false,
    polyline: false,
    marker: false,
    circle: false,
    circlemarker: false,
    rectangle: {
      shapeOptions: {
        color: "#1d4ed8",
        weight: 2,
      },
    },
  },
  edit: {
    featureGroup: drawnItems,
    edit: false,
    remove: false,
  },
});
map.addControl(drawControl);

const polygonLayer = L.geoJSON(null, {
  style: () => ({
    color: "#2563eb",
    weight: 1,
    fillColor: "#60a5fa",
    fillOpacity: 0.25,
  }),
}).addTo(map);

const arrowLayer = L.geoJSON(null, {
  style: () => ({
    color: "#c0392b",
    weight: 2,
    opacity: 0.9,
  }),
}).addTo(map);

const arrowMarkers = L.layerGroup().addTo(map);

const statusEl = document.getElementById("status");
const tableBody = document.querySelector("#results-table tbody");
const searchForm = document.getElementById("search-form");
const cityInput = document.getElementById("city-input");
const fetchBboxButton = document.getElementById("fetch-bbox");
const exportCsvButton = document.getElementById("export-csv");
const exportGeoJsonButton = document.getElementById("export-geojson");

let lastQuery = null;

function setStatus(message, type = "info") {
  if (!message) {
    statusEl.textContent = "";
    statusEl.className = "";
    return;
  }
  statusEl.textContent = message;
  statusEl.className = type;
}

function createArrowIcon(angle) {
  return L.divIcon({
    html: `<div class="arrow-icon" style="transform: rotate(${angle}deg)">↑</div>`,
    className: "arrow-icon-wrapper",
    iconSize: [24, 24],
    iconAnchor: [12, 12],
  });
}

function formatNumber(value, digits = 2) {
  return Number.parseFloat(value).toFixed(digits);
}

function updateTable(features) {
  tableBody.innerHTML = "";
  if (!features.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = "結果がありません";
    row.appendChild(cell);
    tableBody.appendChild(row);
    return;
  }

  for (const feature of features) {
    const row = document.createElement("tr");
    const name = feature.name || "(名称不明)";
    const lat = formatNumber(feature.lat, 6);
    const lon = formatNumber(feature.lon, 6);
    const orientation = formatNumber(feature.orientation_deg, 1);
    const deviation = formatNumber(feature.deviation_deg, 1);

    row.innerHTML = `
      <td>${name}</td>
      <td>${lat}</td>
      <td>${lon}</td>
      <td>${orientation}</td>
      <td>${deviation}</td>
    `;
    tableBody.appendChild(row);
  }
}

function updateMap(polygons, arrows, features) {
  polygonLayer.clearLayers();
  arrowLayer.clearLayers();
  arrowMarkers.clearLayers();

  if (polygons?.features?.length) {
    polygonLayer.addData(polygons);
  }

  if (arrows?.features?.length) {
    arrowLayer.addData(arrows);
  }

  for (const feature of features) {
    const marker = L.marker([feature.lat, feature.lon], {
      icon: createArrowIcon(feature.orientation_deg),
      interactive: false,
    });
    arrowMarkers.addLayer(marker);
  }
}

async function fetchChurchesByBbox(bbox) {
  setStatus("建物データを取得中…", "loading");
  lastQuery = { bbox };

  try {
    const response = await fetch("/api/churches", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ bbox }),
    });

    if (!response.ok) {
      throw new Error(`サーバーエラー: ${response.status}`);
    }

    const data = await response.json();
    updateMap(data.polygons, data.arrows, data.features);
    updateTable(data.features);
    if (!data.features.length) {
      setStatus("教会・大聖堂は見つかりませんでした。", "info");
    } else {
      setStatus(`${data.features.length} 件の建物を取得しました。`, "success");
    }
  } catch (error) {
    console.error(error);
    setStatus("データ取得中にエラーが発生しました。", "error");
  }
}

map.on(L.Draw.Event.CREATED, (event) => {
  drawnItems.clearLayers();
  drawnItems.addLayer(event.layer);
  const bounds = event.layer.getBounds();
  const bbox = {
    north: bounds.getNorth(),
    south: bounds.getSouth(),
    east: bounds.getEast(),
    west: bounds.getWest(),
  };
  fetchChurchesByBbox(bbox);
});

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = cityInput.value.trim();
  if (!query) {
    setStatus("都市名を入力してください。", "error");
    return;
  }
  setStatus("都市を検索中…", "loading");

  try {
    const response = await fetch(`/api/geocode?query=${encodeURIComponent(query)}`);
    if (!response.ok) {
      throw new Error("都市を取得できませんでした。");
    }
    const data = await response.json();
    const bbox = data.bbox;
    const bounds = L.latLngBounds(
      [bbox.south, bbox.west],
      [bbox.north, bbox.east]
    );
    map.fitBounds(bounds, { padding: [20, 20] });
    fetchChurchesByBbox(bbox);
  } catch (error) {
    console.error(error);
    setStatus("都市の検索に失敗しました。", "error");
  }
});

fetchBboxButton.addEventListener("click", () => {
  const bounds = map.getBounds();
  const bbox = {
    north: bounds.getNorth(),
    south: bounds.getSouth(),
    east: bounds.getEast(),
    west: bounds.getWest(),
  };
  fetchChurchesByBbox(bbox);
});

async function exportData(format) {
  if (!lastQuery) {
    setStatus("まずは検索を実行してください。", "error");
    return;
  }

  setStatus("エクスポートデータを生成中…", "loading");
  try {
    const response = await fetch(`/api/churches/export?format=${format}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(lastQuery),
    });

    if (!response.ok) {
      throw new Error("エクスポートに失敗しました。");
    }

    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download =
      format === "csv" ? "church_orientations.csv" : "church_orientations.geojson";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(url);
    setStatus("エクスポートが完了しました。", "success");
  } catch (error) {
    console.error(error);
    setStatus("エクスポートに失敗しました。", "error");
  }
}

exportCsvButton.addEventListener("click", () => exportData("csv"));
exportGeoJsonButton.addEventListener("click", () => exportData("geojson"));

setStatus("都市を検索するか、矩形を描画して開始してください。", "info");
