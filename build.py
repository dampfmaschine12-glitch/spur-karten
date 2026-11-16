#!/usr/bin/env python3
"""
Baut das Kartenpaket für den Spur-Assistenten aus Geofabrik-Extrakten (.osm.pbf).

Erzeugt genau das Kachelformat, das die App (MapTiles.kt / OfflineMap.fetchTile) selbst über Overpass schreibt:

  Dateiname: "<ix>_<iy>.json" (schlank), "<ix>_<iy>.d.json" (detailliert, Heimatgebiet)
             bzw. "<ix>_<iy>.a.json" (nur Autobahnen, ganz Deutschland)
             ix = floor(lon / 0.25), iy = floor(lat / 0.25)
  Inhalt (Format 2, abwärtskompatibel zu Format 1 – die alten Felder bleiben an ihrer Stelle):
             {"r": [[highway, label, maxspeed, lanes, destination, [lat1e5, lon1e5, ...], {zusatz}], ...],
              "p": [[type, name, lat1e5, lon1e5, extra, {zusatz}], ...],
              "s": [[lat1e5, lon1e5, vonWeg, nachWeg, {zusatz}], ...],   (Wegweiser-Relationen)
              "v": 2, "t": <Millisekunden>}
  {zusatz} ist optional und nutzt Kurz-Schlüssel (ROAD_TAGS / POI_TAGS / SIGN_TAGS – gleich wie in MapTiles.kt).

Zwei Pakete:
  kartenpaket-nds-nrw.zip      schlanke + detaillierte Kacheln (wie bisher, jetzt mit allen Knoten-Details)
  autobahnen-deutschland.zip   ".a.json"-Kacheln: Autobahnen, Rampen, autobahnähnliche Straßen (trunk + motorroad),
                               Anschlussstellen, Raststätten, Wegweiser – für ganz Deutschland

Filter wie die Overpass-Abfragen in OfflineMap.query():
  schlank    (Niedersachsen + NRW): Autobahn/Schnellstraße/Bundesstraße, Ausfahrten, Raststätten,
             Tankstellen, Ladesäulen, Restaurants/Imbisse mit Namen, Orte (Knoten)
  detailliert (Heimatgebiet Nordhorn – Schüttorf): zusätzlich alle Straßen bis Wohnstraße,
             Cafés, Toiletten, Apotheken, Krankenhäuser, Parkplätze, Ortsteile

Ein Weg kommt (wie bei Overpass mit bbox) in jede Kachel, die er berührt – mit voller Geometrie.
Flächen/Relationen bekommen als Punkt die Mitte ihres Begrenzungsrechtecks (wie Overpass "out center").

Aufruf:
  python build.py --out kartenpaket-nds-nrw.zip --out-autobahn autobahnen-deutschland.zip \\
      niedersachsen-latest.osm.pbf nordrhein-westfalen-latest.osm.pbf ... autobahn-germany.osm.pbf
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
REGION_AB = (47.25, 5.75, 55.25, 15.25)     # ganz Deutschland (nur Autobahn-Schicht)

MOTORWAY_KINDS = {"motorway", "motorway_link", "trunk", "trunk_link"}

# OSM-Tag -> Kurz-Schlüssel im 7. Feld einer Straße. MUSS zu MapTiles.roadTags in der App passen.
ROAD_TAGS = {
    "ref": "r", "int_ref": "ir", "name": "n",
    "lanes:forward": "lf", "lanes:backward": "lb",
    "turn:lanes": "tl", "turn:lanes:forward": "tlf", "turn:lanes:backward": "tlb",
    "destination": "d", "destination:forward": "df", "destination:backward": "db",
    "destination:ref": "dr", "destination:ref:forward": "drf", "destination:ref:backward": "drb",
    "destination:lanes": "dl", "destination:lanes:forward": "dlf", "destination:lanes:backward": "dlb",
    "destination:ref:lanes": "drl", "destination:ref:lanes:forward": "drlf", "destination:ref:lanes:backward": "drlb",
    "destination:symbol": "ds", "destination:symbol:lanes": "dsl",
    "destination:colour": "dc", "destination:colour:lanes": "dcl",
    "destination:ref:to": "drt", "destination:ref:to:lanes": "drtl",
    "destination:int_ref": "dir", "destination:arrow": "da", "destination:arrow:lanes": "dal",
    "junction:ref": "jr", "junction": "j",
    "maxspeed:conditional": "mc", "maxspeed:variable": "mv", "maxspeed:hgv": "mh",
    "bridge": "br", "tunnel": "tu", "layer": "ly",
    "placement": "pl", "change:lanes": "cl", "change": "ch",
    "hgv": "hg", "hgv:conditional": "hgc", "maxheight": "mxh", "maxweight": "mxw",
    "construction": "co", "opening_date": "od", "motorroad": "mr", "access": "ac",
}
MINOR_SKIP = {"r", "ir", "n", "br", "tu", "ly", "mr"}  # bei kleinen Straßen weglassen (steht schon im Etikett)
POI_TAGS = {"ref": "r", "name": "n", "exit_to": "et", "destination": "d", "destination:ref": "dr"}
SIGN_TAGS = {"destination": "d", "destination:ref": "dr", "destination:symbol": "ds",
             "colour:back": "c", "colour:text": "ct", "distance": "di"}

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
AB_TILES = set(tiles_of(REGION_AB))


def tag(tags: dict, k: str):
    v = tags.get(k)
    return v if v is not None and v.strip() != "" else None


def is_road(tags: dict, detailed: bool) -> bool:
    hw = tags.get("highway")
    if hw == "construction":
        return tags.get("construction") in MOTORWAY_KINDS
    return hw in (ROAD_DETAIL if detailed else ROAD_SLIM)


def is_ab_road(tags: dict) -> bool:
    """Autobahn-Schicht: Autobahn, Rampen, autobahnähnliche Straße (trunk mit motorroad), Bau davon."""
    hw = tags.get("highway")
    if hw == "construction":
        return tags.get("construction") in MOTORWAY_KINDS
    if hw in ("motorway", "motorway_link", "trunk_link"):
        return True
    return hw == "trunk" and tags.get("motorroad") == "yes"


def is_ab_poi(tags: dict, kind: str) -> bool:
    hw = tags.get("highway")
    return (kind == "n" and hw == "motorway_junction") or hw in REST


def oneway_of(hw: str, tags: dict) -> int:
    v = (tags.get("oneway") or "").strip().lower()
    if v in ("yes", "true", "1"):
        return 1
    if v in ("-1", "reverse"):
        return -1
    if v in ("no", "false", "0"):
        return 0
    if tags.get("junction") == "roundabout":
        return 1
    return 1 if hw in ("motorway", "motorway_link") else 0


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


def road_entry(tags: dict, pts: list[int], wid: int = 0):
    """Wie MapTiles.roadEntry() in der App."""
    hw = tag(tags, "highway")
    label = " ".join(v for v in (tag(tags, "ref"), tag(tags, "name")) if v is not None)
    major = hw in MOTORWAY_KINDS or hw == "construction"
    if label.strip() == "" and not (hw or "").endswith("_link") and hw != "construction":
        return None
    dest = tag(tags, "destination") or tag(tags, "destination:ref")
    x = {}
    for k, s in ROAD_TAGS.items():
        if not major and s in MINOR_SKIP:
            continue
        v = tag(tags, k)
        if v is not None:
            x[s] = v
    ow = oneway_of(hw, tags)
    if ow != (1 if hw in ("motorway", "motorway_link") else 0):
        x["o"] = ow
    if major and wid:
        x["i"] = wid
    e = [hw, label, tag(tags, "maxspeed") or "", tag(tags, "lanes") or "", dest or "", pts]
    if x:
        e.append(x)
    return e


def poi_entry(tags: dict, lat: float, lon: float, oid: int = 0):
    """Wie MapTiles.poiEntry() in der App."""
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
    e = [typ, name, jround(lat * 1e5), jround(lon * 1e5), extra]
    if typ in ("motorway_junction", "services", "rest_area"):
        x = {s: tag(tags, k) for k, s in POI_TAGS.items() if tag(tags, k) is not None}
        if oid:
            x["i"] = oid
        if x:
            e.append(x)
    return e


class Collector:
    def __init__(self):
        # (ix, iy, schicht) -> {key: eintrag}; schicht: True = detailliert, False = schlank, "a" = Autobahn
        self.roads: dict[tuple, dict] = {}
        self.pois: dict[tuple, dict] = {}
        self.signs: dict[tuple, dict] = {}
        self.road_ids: set[int] = set()   # alle aufgenommenen Wege (für Wegweiser-Relationen)
        self.ab_ids: set[int] = set()
        self.stats = {"roads": 0, "pois": 0, "signs": 0}

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
            e = poi_entry(tags, lat, lon, int(key[1:]))
            if e is None:
                continue
            self.pois.setdefault((x, y, detailed), {})[key] = e
            self.stats["pois"] += 1
        if (x, y) in AB_TILES and is_ab_poi(tags, kind):
            e = poi_entry(tags, lat, lon, int(key[1:]))
            if e is not None:
                self.pois.setdefault((x, y, "a"), {})[key] = e
                self.stats["pois"] += 1

    def add_sign(self, key: str, tags: dict, lat: float, lon: float, frm: int, to: int):
        """Wegweiser-Relation: nur, wenn einer der Wege im Paket ist."""
        x = {s: tag(tags, k) for k, s in SIGN_TAGS.items() if tag(tags, k) is not None}
        if "d" not in x and "dr" not in x:
            return
        e = [jround(lat * 1e5), jround(lon * 1e5), frm, to, x]
        tx, ty = tile_ix(lon), tile_iy(lat)
        if frm in self.road_ids or to in self.road_ids:
            for detailed in self._targets(tx, ty):
                self.signs.setdefault((tx, ty, detailed), {})[key] = e
                self.stats["signs"] += 1
        if (frm in self.ab_ids or to in self.ab_ids) and (tx, ty) in AB_TILES:
            self.signs.setdefault((tx, ty, "a"), {})[key] = e
            self.stats["signs"] += 1

    def add_road(self, key: str, tags: dict, coords: list[tuple[float, float]]):
        if not (is_road(tags, True) or is_ab_road(tags)):
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
        ab = is_ab_road(tags)
        wid = int(key[1:])
        for (x, y) in tiles:
            layers = [d for d in self._targets(x, y) if is_road(tags, d)]
            if ab and (x, y) in AB_TILES:
                layers.append("a")
            for layer in layers:
                if entry is None:
                    pts = []
                    for lat, lon in coords:
                        pts.append(jround(lat * 1e5))
                        pts.append(jround(lon * 1e5))
                    entry = road_entry(tags, pts, wid)
                    if entry is None:
                        return
                self.roads.setdefault((x, y, layer), {})[key] = entry
                self.stats["roads"] += 1
                self.road_ids.add(wid)
                if layer == "a":
                    self.ab_ids.add(wid)


def relevant(tags: dict, kind: str) -> tuple[bool, bool]:
    """(ist Straße, ist Punkt) – großzügig (detailliert), genau gefiltert wird pro Kachel."""
    return (is_road(tags, True) or is_ab_road(tags)) and kind == "w", is_poi(tags, kind, True)


def process_file(path: str, col: Collector):
    t0 = time.time()
    keys = osmium.filter.KeyFilter("highway", "amenity", "place")

    # 1) Relationen: passende merken, dazu ihre Mitglieder
    rels = {}
    signs = {}   # Wegweiser-Relationen: id -> (tags, vonWeg, nachWeg, [Knoten], [Wege])
    member_ways: set[int] = set()
    member_nodes: set[int] = set()
    for o in osmium.FileProcessor(path, osmium.osm.RELATION).with_filter(osmium.filter.KeyFilter("highway", "amenity", "place", "type")):
        tags = dict(o.tags)
        if tags.get("type") == "destination_sign":
            frm = to = 0
            nodes, ways = [], []
            for m in o.members:
                if m.type == "w" and m.role == "from":
                    frm = m.ref
                elif m.type == "w" and m.role == "to":
                    to = m.ref
                elif m.type == "n" and m.role == "intersection":
                    nodes.insert(0, m.ref)   # Kreuzung zuerst
                elif m.type == "n" and m.role == "sign":
                    nodes.append(m.ref)      # sonst der Standort des Schilds
            if to:
                ways.append(to)
            if frm:
                ways.append(frm)
            if (frm or to) and (nodes or ways):
                signs[o.id] = (tags, frm, to, nodes, ways)
                member_nodes.update(nodes)
                member_ways.update(ways)
            continue
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
    way_first: dict[int, tuple[float, float]] = {}
    if rels or signs:
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
                    way_first[o.id] = coords[0]

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

    # 4) Wegweiser-Relationen: an die Kreuzung (sonst Schild, sonst Anfang des Zielwegs) setzen
    for rid, (tags, frm, to, nodes, ways) in signs.items():
        pos = next((node_loc[n] for n in nodes if n in node_loc), None) or next((way_first[w] for w in ways if w in way_first), None)
        if pos:
            col.add_sign("r%d" % rid, tags, pos[0], pos[1], frm, to)

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


def write_pack(out: str, manifest_path, name: str, tiles, col: Collector, pbf, now, osm_stand, datum, skip_empty: bool):
    """Schreibt ein Paket. tiles: Liste (x, y, schicht)."""
    t_ms = int(now.timestamp() * 1000)
    files = []
    tmp = out + ".tmp"
    total = 0
    suffix = {True: ".d.json", False: ".json", "a": ".a.json"}
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for (x, y, layer) in tiles:
            k = (x, y, layer)
            r = list(col.roads.get(k, {}).values())
            p = list(col.pois.get(k, {}).values())
            s = list(col.signs.get(k, {}).values())
            if skip_empty and not (r or p):
                continue
            tile = {"r": r, "p": p, "v": 2, "t": t_ms}
            if s:
                tile["s"] = s
            data = json.dumps(tile, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            fname = f"{x}_{y}" + suffix[layer]
            z.writestr(fname, data)
            files.append({"datei": fname, "bytes": len(data), "wege": len(r), "punkte": len(p), "wegweiser": len(s)})
            total += len(data)
        manifest = {
            "format": 2,
            "name": name,
            "datum": datum,
            "osm_stand": osm_stand,
            "erstellt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kachel_groesse": TILE,
            "gebiet_schlank": list(REGION_SLIM),
            "gebiet_detailliert": list(REGION_DETAIL),
            "gebiet_autobahn": list(REGION_AB),
            "quellen": [os.path.basename(q) for q in pbf],
            "anzahl": len(files),
            "bytes": total,
            "kacheln": files,
            "lizenz": "Kartendaten © OpenStreetMap-Mitwirkende, ODbL 1.0",
        }
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"))
    os.replace(tmp, out)
    if manifest_path:
        manifest["zip_bytes"] = os.path.getsize(out)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
    filled = sum(1 for f in files if f["wege"] or f["punkte"])
    print(f"Fertig: {out} – {len(files)} Kacheln ({filled} mit Daten), "
          f"{total / 1e6:.1f} MB entpackt, {os.path.getsize(out) / 1e6:.1f} MB gepackt, Stand {datum}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Kartenpaket für den Spur-Assistenten bauen")
    ap.add_argument("pbf", nargs="+", help="Geofabrik-Extrakte (.osm.pbf)")
    ap.add_argument("--out", default="kartenpaket-nds-nrw.zip", help="Ziel-ZIP (schlank + detailliert); leer = nicht bauen")
    ap.add_argument("--manifest", default=None, help="manifest.json zusätzlich hierhin schreiben")
    ap.add_argument("--out-autobahn", default=None, help="Ziel-ZIP der Autobahn-Schicht (ganz Deutschland)")
    ap.add_argument("--manifest-autobahn", default=None, help="Manifest der Autobahn-Schicht zusätzlich hierhin schreiben")
    ap.add_argument("--gebiet-schlank", default=None, help="nur für Tests: s,w,n,e statt Niedersachsen + NRW")
    ap.add_argument("--gebiet-detail", default=None, help="nur für Tests: s,w,n,e statt Nordhorn – Schüttorf")
    ap.add_argument("--gebiet-autobahn", default=None, help="nur für Tests: s,w,n,e statt ganz Deutschland")
    args = ap.parse_args()

    global REGION_SLIM, REGION_DETAIL, REGION_AB, SLIM_TILES, DETAIL_TILES, AB_TILES
    if args.gebiet_schlank:
        REGION_SLIM = tuple(float(v) for v in args.gebiet_schlank.split(","))
        SLIM_TILES = set(tiles_of(REGION_SLIM))
    if args.gebiet_detail:
        REGION_DETAIL = tuple(float(v) for v in args.gebiet_detail.split(","))
        DETAIL_TILES = set(tiles_of(REGION_DETAIL))
    if args.gebiet_autobahn:
        REGION_AB = tuple(float(v) for v in args.gebiet_autobahn.split(","))
        AB_TILES = set(tiles_of(REGION_AB))
    if not args.out_autobahn:
        AB_TILES = set()

    col = Collector()
    print("Lese Extrakte …", flush=True)
    for p in args.pbf:
        process_file(p, col)

    now = dt.datetime.now(dt.timezone.utc)
    osm_stand = osm_timestamp(args.pbf)
    datum = (osm_stand[:10] if osm_stand else now.strftime("%Y-%m-%d"))
    order = lambda t: (t[1], t[0])
    if args.out:
        tiles = [(x, y, (x, y) in DETAIL_TILES) for (x, y) in sorted(SLIM_TILES | DETAIL_TILES, key=order)]
        write_pack(args.out, args.manifest, "kartenpaket-nds-nrw", tiles, col, args.pbf, now, osm_stand, datum, skip_empty=False)
    if args.out_autobahn:
        tiles = [(x, y, "a") for (x, y) in sorted(AB_TILES, key=order)]
        write_pack(args.out_autobahn, args.manifest_autobahn, "autobahnen-deutschland", tiles, col, args.pbf, now, osm_stand, datum, skip_empty=True)
    print(f"{col.stats['roads']} Weg-, {col.stats['pois']} Punkt- und {col.stats['signs']} Wegweiser-Einträge", flush=True)


if __name__ == "__main__":
    sys.exit(main())
