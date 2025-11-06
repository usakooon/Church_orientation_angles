# Church Orientation Explorer

教会建物の向きを自動計算するミニ Web アプリです。OpenStreetMap/Overpass API から建物ポリゴンを取得し、最小回転外接矩形に基づく長辺の方位角と東西軸からの偏差を計算して可視化・エクスポートできます。

## 主な機能

- 左パネルに Leaflet ベースの OSM マップを表示
  - 都市名検索 (Nominatim) によるエリア指定
  - Leaflet.draw による矩形選択
  - 現在の表示範囲から直接検索
- building=church / building=cathedral を対象に Overpass 経由でポリゴンを取得 (WGS84)
- GeoPandas + Shapely で以下を算出
  - point_on_surface に基づく代表点 (緯度・経度)
  - oriented minimum bounding rectangle の長辺方位角 (0–360°, 北=0°)
  - 東西軸からの偏差: `min(|orientation-90|, |orientation-270|)`
- 右パネルに name / lat / lon / orientation_deg / deviation_deg のテーブル表示
- 地図に建物ポリゴンと方位矢印を重ねて表示
- CSV / GeoJSON でのエクスポート

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 起動方法

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
# もしくは uvicorn を別途インストールせずに次のように起動できます
python -m app.main
```

ブラウザで `http://localhost:8000/` にアクセスするとアプリを利用できます。ワーキングディレクトリに依存せず動作するように静的ファイルとテンプレートへのパスはアプリ内部で解決されます。

## 動作確認

Duomo di Milano を検索すると、周辺の教会・大聖堂が取得され、向き矢印とテーブルが表示されます。矩形選択・表示範囲検索でも同様に利用できます。
