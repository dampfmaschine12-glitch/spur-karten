# spur-karten – Kartenpaket für den Spur-Assistenten

Dieses Repo baut einmal im Monat automatisch das **Kartenpaket Niedersachsen + NRW** für die App
„Spur-Assistent“ und stellt es als Download bereit. Die App lädt damit eine einzige Datei
(einige zehn MB, wenige Minuten im WLAN) statt 360 Kartenteile einzeln über Overpass (über einen Tag).

Das Repo muss **öffentlich** sein, damit die App die Datei ohne Anmeldung laden kann.

## Einrichten (einmalig)

1. Auf GitHub ein neues, **öffentliches** Repo `spur-karten` anlegen
   (Besitzer `dampfmaschine12-glitch`, sonst die Adresse in der App ändern, siehe unten).
2. Den Inhalt dieses Ordners 1:1 hineinkopieren – **auch den versteckten Ordner `.github`**:
   ```
   git clone https://github.com/dampfmaschine12-glitch/spur-karten.git
   cp -r Spur-assistent-/kartenpaket/. spur-karten/
   cd spur-karten && git add -A && git commit -m "Kartenpaket bauen" && git push
   ```
3. Auf GitHub im Repo: **Actions → „Kartenpaket bauen“ → „Run workflow“**. Dauert ca. 20–40 Minuten.
4. Fertig. Unter **Releases → „kartenpaket“** liegen jetzt `kartenpaket-nds-nrw.zip` und `manifest.json`.
   Ab dann baut GitHub das Paket jeden Monat (am 3.) automatisch neu und ersetzt die alten Dateien.

Falls Actions im neuen Repo keine Release-Dateien anlegen darf: *Settings → Actions → General →
Workflow permissions → „Read and write permissions“* einschalten.

## Was die App macht

- Adresse: `https://github.com/dampfmaschine12-glitch/spur-karten/releases/download/kartenpaket/kartenpaket-nds-nrw.zip`
  (Konstante `MapPackage.PACKAGE_URL` in `app/src/main/java/de/michael/spurassistent/data/MapPackage.kt`).
- Nur im WLAN zu Hause (oder nach „Jetzt laden“). Der Download wird in eine Datei gestreamt und
  bei Abbruch fortgesetzt. Danach wird entpackt; vorhandene ausführliche Kartenteile bleiben erhalten.
- Einmal im Monat prüft die App, ob es eine neue Version gibt (ETag/Größe der Datei), und lädt sie dann neu.
- Gibt es das Paket nicht (Repo fehlt, noch nicht gebaut), lädt die App wie früher Kartenteile einzeln über Overpass,
  jetzt aber auf 3 Server verteilt.

## Inhalt des Pakets

`kartenpaket-nds-nrw.zip` enthält:

- `<ix>_<iy>.json` – schlanke Kartenteile (0,25° × 0,25°) für das Gebiet 50,25–54,0° N / 5,75–11,75° O:
  Autobahnen, Schnell- und Bundesstraßen, Ausfahrten, Raststätten, Tankstellen, Ladesäulen,
  Restaurants/Imbisse mit Namen, Städte und Dörfer.
- `<ix>_<iy>.d.json` – ausführliche Kartenteile fürs Heimatgebiet Nordhorn – Schüttorf
  (52,25–52,50° N / 6,75–7,50° O): zusätzlich alle Straßen, Cafés, Toiletten, Apotheken,
  Krankenhäuser, Parkplätze, Ortsteile.
- `manifest.json` – Datum (`datum`), OSM-Stand, Liste der Kacheln mit Größe.

Das Format ist genau das, was die App selbst aus Overpass-Antworten schreibt
(`{"r":[[highway,label,maxspeed,lanes,destination,[lat·1e5,lon·1e5,…]],…],"p":[[typ,name,lat·1e5,lon·1e5,extra],…],"t":ms}`).

## Lokal bauen

```
pip install -r requirements.txt
curl -O https://download.geofabrik.de/europe/germany/bremen-latest.osm.pbf
python build.py --out kartenpaket-nds-nrw.zip --manifest manifest.json bremen-latest.osm.pbf
```

Mit `osmium-tool` lassen sich große Extrakte vorher verkleinern (siehe Workflow). Welche Geofabrik-Extrakte
verwendet werden, steht in `.github/workflows/karten-bauen.yml` (`EXTRAKTE`). Neben Niedersachsen, NRW und Bremen
sind die Nachbarn dabei, damit die Kartenteile an den Grenzen (z. B. Nordhorn – Niederlande) vollständig sind.

## Lizenz der Daten

Kartendaten © OpenStreetMap-Mitwirkende, verfügbar unter der Open Database License (ODbL 1.0),
bezogen über [Geofabrik](https://download.geofabrik.de/).
