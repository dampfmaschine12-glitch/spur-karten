#!/usr/bin/env python3
"""
Baut das Kartenpaket für den Spur-Assistenten aus Geofabrik-Extrakten (.osm.pbf).

Erzeugt genau das Kachelformat, das die App (OfflineMap.fetchTile) selbst über Overpass schreibt:

  Dateiname: "<ix>_<iy>.json" (schlank) bzw. "<ix>_<iy>.d.json" (detailliert, Heimatgebiet)
             ix = floor(lon / 0.25), iy = floor(lat / 0.25)
  Inhalt:    {"r": [[highway, label, maxspeed, lanes, destination, [lat1e5, lon1e5, ...]], ...],
              "p": [[type, name, lat1e5, lon1e5, extra], ...],
              "t": <Millisekunden>}

Filter wie die Overpass-Abfragen in OfflineMap.query():
  schlank    (Niedersachsen + NRW): Autobahn/Schnellstraße/Bundesstraße, Ausfahrten, Raststätten,
             Tankstellen, Ladesäulen, Restaurants/Imbisse mit Namen, Orte (Knoten)
  detailliert (Heimatgebiet Nordhorn – Schüttorf): zusätzlich alle Straßen bis Wohnstraße,
             Cafés, Toiletten, Apotheken, Krankenhäuser, Parkplätze, Ortsteile

Ein Weg kommt (wie bei Overpass mit bbox) in jede Kachel, die er berührt – mit voller Geometrie.
Flächen/Relationen bekommen als Punkt die Mitte ihres Begrenzungsrechtecks (wie Overpass "out center").

Aufruf:
  python build.py --out kartenpaket-nds-nrw.zip niedersachsen-latest.osm.pbf nordrhein-westfalen-latest.osm.pbf ...
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import zipfile

import osmium
import osmium.filter

TILE = 0.25

# Gleiche Gebiete wie OfflineMap.regions in der App (s, w, n, e)
REGION_SLIM = (50.25, 5.75, 54.0, 11.75)   # Niedersachsen + Nordrhein-Westfalen
REGION_DETAIL = (52.25, 6.75, 52.50, 7.50)  # Nordhorn – Schüttorf

ROAD_SLIM = {"motorway", "motorway_link", "trunk", "trunk_link", "primary"}
ROAD_DETAIL = ROAD_SLIM | {"primary_link", "secondary", "tertiary", "unclassified", "residential", "living_street"}

REST = {"services", "rest_area"}
FUEL_SLIM = {"fuel", "charging_station"}
FUEL_DETAIL = {"fuel", "charging_station", "toilets"}
NAMED_SLIM = {"restaurant", "fast_food"}
NAMED_DETAIL = {"restaurant", "fast_food", "cafe", "toilets", "pharmacy", "hospital", "parking"}
PLACE_SLIM = {"city", "town", "village"}            # nur Knoten
PLACE_DETAIL = {"city", "town", "village", "suburb"}  # Knoten, Wege, Relationen


def jround(x: float) -> int:
    """Wie Java Math.round (halbe Werte aufrunden)."""
    return int(math.floor(x + 0.5))


def tile_ix(lon: float) -> int:
    return int(math.floor(lon / TILE))


def tile_iy(lat: float) -> int:
    return int(math.floor(lat / TILE))


def tiles_of(region) -> list[tuple[int, int]]:
    """Wie OfflineMap.tilesOf()."""
    s, w, n, e = region
    return [(x, y) for y in range(tile_iy(s), tile_iy(n - 1e-9) + 1) for x in range(tile_ix(w), tile_ix(e - 1e-9) + 1)]


SLIM_TILES = set(tiles_of(REGION_SLIM))
DETAIL_TILES = set(tiles_of(REGION_DETAIL))


def tag(tags: dict, k: str):
    v = tags.get(k)
    return v if v is not None and v.strip() != "" else None


def is_road(tags: dict, detailed: bool) -> bool:
    return tags.get("highway") in (ROAD_DETAIL if detailed else ROAD_SLIM)


def is_poi(tags: dict, kind: str, detailed: bool) -> bool:
    """kind: 'n', 'w' oder 'r'. Entspricht den Zeilen 2 und 3 der Overpass-Abfrage."""
    hw = tags.get("highway")
    am = tags.get("amenity")
    pl = tags.get("place")
    if kind == "n" and hw == "motorway_junction":
        return True
    if hw in REST:
        return True
    if am in (FUEL_DETAIL if detailed else FUEL_SLIM):
        return True
    if am in (NAMED_DETAIL if detailed else NAMED_SLIM) and "name" in tags:
        return True
    if detailed:
        return pl in PLACE_DETAIL
    return kind == "n" and pl in PLACE_SLIM


def road_entry(tags: dict, pts: list[int]):
    """Wie der Weg-Zweig in OfflineMap.fetchTile()."""
    hw = tag(tags, "highway")
    label = " ".join(v for v in (tag(tags, "ref"), tag(tags, "name")) if v is not None)
    if label.strip() == "" and not (hw or "").endswith("_link"):
        return None
    dest = tag(tags, "destination") or tag(tags, "destination:ref")
    return [hw, label, tag(tags, "maxspeed") or "", tag(tags, "lanes") or "", dest or "", pts]


def poi_entry(tags: dict, lat: float, lon: float):
    """Wie der Punkt-Zweig in OfflineMap.fetchTile()."""
    typ = tag(tags, "highway")
    if typ is None:
        am = tag(tags, "amenity")
        typ = "a:" + am if am is not None else None
    if typ is None:
        pl = tag(tags, "place")
        typ = "p:" + pl if pl is not None else None
    if typ is None:
        return None
    if typ == "motorway_junction":
        ref = tag(tags, "ref")
        name = " ".join(v for v in (("Ausfahrt " + ref) if ref else None, tag(tags, "name")) if v is not None)
        if name.strip() == "":
            name = "Ausfahrt"
    elif typ == "a:charging_station":
        parts = []
        for v in (tag(tags, "operator") or tag(tags, "brand"), tag(tags, "name")):
            if v is not None and v not in parts:
                parts.append(v)
        name = " ".join(parts)
    else:
        name = tag(tags, "name") or tag(tags, "brand") or ""
    power = next((tag(tags, k) for k in ("socket:type2_combo:output", "socket:ccs:output", "charging_station:output") if tag(tags, k)), None)
    oh = tag(tags, "opening_hours")
    st = tag(tags, "addr:street")
    addr = None
    if st is not None:
        hn = tag(tags, "addr:housenumber")
        city = tag(tags, "addr:city")
        addr = st + (" " + hn if hn else "") + (", " + city if city else "")
    extra = ", ".join(v for v in (tag(tags, "destination"), power, ("Öffnungszeiten " + oh) if oh else None, addr) if v is not None)
    return [typ, name, jround(lat * 1e5), jround(lon * 1e5), extra]


class Collector:
    def __init__(self):
        # (ix, iy, detailed) -> {key: eintrag}
        self.roads: dict[tuple[int, int, bool], dict] = {}
        self.pois: dict[tuple[int, int, bool], dict] = {}
        self.stats = {"roads": 0, "pois": 0}

    def _targets(self, x: int, y: int):
        out = []
        if (x, y) in DETAIL_TILES:
            out.append(True)
        elif (x, y) in SLIM_TILES:
            out.append(False)
        return out

    def add_poi(self, key: str, tags: dict, kind: str, lat: float, lon: float):
        x, y = tile_ix(lon), tile_iy(lat)
        for detailed in self._targets(x, y):
            if not is_poi(tags, kind, detailed):
                continue
            e = poi_entry(tags, lat, lon)
            if e is None:
                continue
            self.pois.setdefault((x, y, detailed), {})[key] = e
            self.stats["pois"] += 1

    def add_road(self, key: str, tags: dict, coords: list[tuple[float, float]]):
        if not (is_road(tags, True)):
            return
        tiles = set()
        prev = None
        for lat, lon in coords:
            tiles.add((tile_ix(lon), tile_iy(lat)))
            if prev is not None:
                # Strecke über Kachelgrenze: alle Kacheln im Rechteck der Strecke (Strecken sind kurz)
                x0, x1 = sorted((tile_ix(prev[1]), tile_ix(lon)))
                y0, y1 = sorted((tile_iy(prev[0]), tile_iy(lat)))
                if x0 != x1 or y0 != y1:
                    for xx in range(x0, x1 + 1):
                        for yy in range(y0, y1 + 1):
                            tiles.add((xx, yy))
            prev = (lat, lon)
        entry = None
        for (x, y) in tiles:
            for detailed in self._targets(x, y):
                if not is_road(tags, detailed):
                    continue
                if entry is None:
                    pts = []
                    for lat, lon in coords:
                        pts.append(jround(lat * 1e5))
                        pts.append(jround(lon * 1e5))
                    entry = road_entry(tags, pts)
                    if entry is None:
                        return
                self.roads.setdefault((x, y, detailed), {})[key] = entry
                self.stats["roads"] += 1


def relevant(tags: dict, kind: str) -> tuple[bool, bool]:
    """(ist Straße, ist Punkt) – großzügig (detailliert), genau gefiltert wird pro Kachel."""
    return is_road(tags, True) and kind == "w", is_poi(tags, kind, True)


def process_file(path: str, col: Collector):
    t0 = time.time()
    keys = osmium.filter.KeyFilter("highway", "amenity", "place")

    # 1) Relationen: passende merken, dazu ihre Mitglieder
    rels = {}
    member_ways: set[int] = set()
    member_nodes: set[int] = set()
    for o in osmium.FileProcessor(path, osmium.osm.RELATION).with_filter(keys):
        tags = dict(o.tags)
        if not is_poi(tags, "r", True):
            continue
        mw, mn = [], []
        for m in o.members:
            if m.type == "w":
                mw.append(m.ref)
            elif m.type == "n":
                mn.append(m.ref)
        rels[o.id] = (tags, mw, mn)
        member_ways.update(mw)
        member_nodes.update(mn)

    # 2) Knoten und Wege mit passenden Schlüsseln (Filter läuft in C++, Koordinaten aller Knoten werden gemerkt)
    nodes_seen = ways_seen = 0
    fp = osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY).with_locations().with_filter(keys)
    for o in fp:
        tags = dict(o.tags)
        if o.is_node():
            nodes_seen += 1
            if o.location.valid():
                col.add_poi("n%d" % o.id, tags, "n", o.location.lat, o.location.lon)
            continue
        ways_seen += 1
        road, poi = relevant(tags, "w")
        if not (road or poi):
            continue
        coords = [(n.location.lat, n.location.lon) for n in o.nodes if n.location.valid()]
        if not coords:
            continue
        if road:
            col.add_road("w%d" % o.id, tags, coords)
        if poi:
            lats = [c[0] for c in coords]
            lons = [c[1] for c in coords]
            col.add_poi("w%d" % o.id, tags, "w", (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)

    # 2b) Mitglieder der Relationen (auch Wege ohne passende Schlüssel, z. B. Außenringe von Flächen)
    way_bbox: dict[int, tuple[float, float, float, float]] = {}
    node_loc: dict[int, tuple[float, float]] = {}
    if rels:
        ids = osmium.filter.IdFilter(member_ways | member_nodes)
        for o in osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY).with_locations().with_filter(ids):
            if o.is_node():
                if o.id in member_nodes and o.location.valid():
                    node_loc[o.id] = (o.location.lat, o.location.lon)
            elif o.id in member_ways:
                coords = [(n.location.lat, n.location.lon) for n in o.nodes if n.location.valid()]
                if coords:
                    lats = [c[0] for c in coords]
                    lons = [c[1] for c in coords]
                    way_bbox[o.id] = (min(lats), min(lons), max(lats), max(lons))

    # 3) Relationen: Mitte des Begrenzungsrechtecks aller Mitglieder
    for rid, (tags, mw, mn) in rels.items():
        lats, lons = [], []
        for w in mw:
            b = way_bbox.get(w)
            if b:
                lats += [b[0], b[2]]
                lons += [b[1], b[3]]
        for n in mn:
            p = node_loc.get(n)
            if p:
                lats.append(p[0])
                lons.append(p[1])
        if not lats:
            continue
        col.add_poi("r%d" % rid, tags, "r", (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)

    print(f"  {os.path.basename(path)}: {nodes_seen} Knoten, {ways_seen} Wege, {len(rels)} Relationen geprüft "
          f"({time.time() - t0:.0f} s)", flush=True)


def osm_timestamp(paths: list[str]) -> str:
    stamps = []
    for p in paths:
        try:
            h = osmium.io.Reader(p, osmium.osm.osm_entity_bits.NOTHING).header()
            v = h.get("osmosis_replication_timestamp")
            if v:
                stamps.append(v)
        except Exception:
            pass
    return min(stamps) if stamps else ""


def main():
    ap = argparse.ArgumentParser(description="Kartenpaket für den Spur-Assistenten bauen")
    ap.add_argument("pbf", nargs="+", help="Geofabrik-Extrakte (.osm.pbf)")
    ap.add_argument("--out", default="kartenpaket-nds-nrw.zip", help="Ziel-ZIP")
    ap.add_argument("--manifest", default=None, help="manifest.json zusätzlich hierhin schreiben")
    ap.add_argument("--gebiet-schlank", default=None, help="nur für Tests: s,w,n,e statt Niedersachsen + NRW")
    ap.add_argument("--gebiet-detail", default=None, help="nur für Tests: s,w,n,e statt Nordhorn – Schüttorf")
    args = ap.parse_args()

    global REGION_SLIM, REGION_DETAIL, SLIM_TILES, DETAIL_TILES
    if args.gebiet_schlank:
        REGION_SLIM = tuple(float(v) for v in args.gebiet_schlank.split(","))
        SLIM_TILES = set(tiles_of(REGION_SLIM))
    if args.gebiet_detail:
        REGION_DETAIL = tuple(float(v) for v in args.gebiet_detail.split(","))
        DETAIL_TILES = set(tiles_of(REGION_DETAIL))

    col = Collector()
    print("Lese Extrakte …", flush=True)
    for p in args.pbf:
        process_file(p, col)

    now = dt.datetime.now(dt.timezone.utc)
    osm_stand = osm_timestamp(args.pbf)
    datum = (osm_stand[:10] if osm_stand else now.strftime("%Y-%m-%d"))
    t_ms = int(now.timestamp() * 1000)

    files = []
    tmp = args.out + ".tmp"
    total = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for (x, y) in sorted(SLIM_TILES | DETAIL_TILES, key=lambda t: (t[1], t[0])):
            detailed = (x, y) in DETAIL_TILES
            k = (x, y, detailed)
            r = list(col.roads.get(k, {}).values())
            p = list(col.pois.get(k, {}).values())
            data = json.dumps({"r": r, "p": p, "t": t_ms}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            name = f"{x}_{y}" + (".d.json" if detailed else ".json")
            z.writestr(name, data)
            files.append({"datei": name, "bytes": len(data), "wege": len(r), "punkte": len(p)})
            total += len(data)
        manifest = {
            "format": 1,
            "name": "kartenpaket-nds-nrw",
            "datum": datum,
            "osm_stand": osm_stand,
            "erstellt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kachel_groesse": TILE,
            "gebiet_schlank": list(REGION_SLIM),
            "gebiet_detailliert": list(REGION_DETAIL),
            "quellen": [os.path.basename(p) for p in args.pbf],
            "anzahl": len(files),
            "bytes": total,
            "kacheln": files,
            "lizenz": "Kartendaten © OpenStreetMap-Mitwirkende, ODbL 1.0",
        }
        mjson = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
        z.writestr("manifest.json", mjson)
    os.replace(tmp, args.out)
    if args.manifest:
        manifest["zip_bytes"] = os.path.getsize(args.out)
        with open(args.manifest, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
    filled = sum(1 for f in files if f["wege"] or f["punkte"])
    print(f"Fertig: {args.out} – {len(files)} Kacheln ({filled} mit Daten), "
          f"{total / 1e6:.1f} MB entpackt, {os.path.getsize(args.out) / 1e6:.1f} MB gepackt, "
          f"{col.stats['roads']} Weg- und {col.stats['pois']} Punkteinträge, Stand {datum}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
