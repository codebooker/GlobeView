<div align="center">
  <img src="docs/globeview-banner.svg" alt="GlobeView — a live, explorable world" width="100%">

  <p><strong>One globe. The world in motion.</strong></p>
  <p>Explore aircraft, vessels, weather, hazards, infrastructure, radio, and public cameras on a single interactive map.</p>

  <p>
    <a href="#run-it-locally">Run locally</a> ·
    <a href="#explore">Explore</a> ·
    <a href="#how-it-works">How it works</a> ·
    <a href="docs/FEEDS.md">Data sources</a>
  </p>
</div>

---

## Explore

| In the sky | On the water | On the ground |
| :--- | :--- | :--- |
| Live aircraft, callsign and tail-number search, observed flight trails, and available airport routes | AIS vessel positions and name search, ports, shipping lanes, and marine forecasts | Road incidents and cameras, rail lines, power infrastructure, outages, and public webcams |

| Around the planet | In the atmosphere | At your fingertips |
| :--- | :--- | :--- |
| Earthquakes, fires, floods, volcanoes, tropical cyclones, and model guidance | Satellite basemaps, GOES clouds, weather radar, alerts, and a day/night boundary | Place search, trip planning, private map drawings, cyber signals, and radio from around the world |

GlobeView starts as a **spherical MapLibre globe** with useful layers already on. Rotate it, switch to 3D terrain, choose a satellite basemap, or turn whole layer groups on and off. Selecting an aircraft starts tracking it; selecting a storm opens its available guidance. Map notes stay in your browser.

## Run it locally

Requires Python 3.9+ and a modern browser. No frontend build step or paid map key is needed.

```bash
python3 -m pip install --requirement requirements.txt
cp .env.example .env
bash start.sh
```

Open **http://localhost:8765/**. To choose another port, set `AMERICAMAP_PORT=8766` before starting the server. The inherited `AMERICAMAP_` environment names remain in use by the feed adapter.

Most layers work without credentials. For live AIS vessel positions, add `AISSTREAM_API_KEY` to the server's `.env`; the key never goes to the browser. Optional state traffic keys are documented in [`.env.example`](.env.example).

## How it works

```text
Browser (MapLibre + plain JavaScript)
           │
           ├── public map tiles and selected direct feeds
           │
           └── Python proxy ── shared caches ── public data providers
                                └── one AISStream connection
```

The [Python server](proxy.py) serves the app and normalizes external feeds. It shares cached responses across viewers, limits upstream concurrency, and opens one AISStream subscription for active map areas. [MapLibre](https://maplibre.org/) renders the globe; the client is plain JavaScript and CSS. Pins, labels, lines, polygons, and radius circles are stored in browser `localStorage`, with no server sync.

The quick route planner uses public Valhalla and OSRM demo services. For a larger deployment, use your own routing backend and place a caching reverse proxy in front of the app. See [data sources and coverage](docs/FEEDS.md) for provider details, update intervals, licenses, and limits.

## Production deployment

Pull requests and pushes to `main` run the test workflow. A passing push to `main` deploys that exact commit to Hetzner through a signed HTTPS hook. Server provisioning, firewall, HTTPS, rollback behavior, and the required GitHub deployment secret are documented in [`deploy/README.md`](deploy/README.md).

## Project layout

| Path | Purpose |
| :--- | :--- |
| [`index.html`](index.html), [`ui.css`](ui.css), [`globe.js`](globe.js) | Globe UI, layers, controls, and popups |
| [`proxy.py`](proxy.py) | HTTP server, regional feeds, caches, and camera relay |
| [`global_feeds.py`](global_feeds.py) | Aircraft and AIS vessel data |
| [`hazard_feeds.py`](hazard_feeds.py), [`cyclone_guidance.py`](cyclone_guidance.py) | Natural hazards and storm tracks |
| [`international_emergency.py`](international_emergency.py), [`international_infrastructure.py`](international_infrastructure.py) | Regional public alerts, roads, and outages |
| [`radio_catalog.py`](radio_catalog.py), [`cyber_feeds.py`](cyber_feeds.py), [`arcgis_catalog.py`](arcgis_catalog.py) | Radio, cyber, and infrastructure layers |
| [`trip.js`](trip.js), [`trip_routing.py`](trip_routing.py), [`drawings.js`](drawings.js) | Routes and private map drawings |

## A note on live data

Coverage and freshness depend on each provider. A map marker can be an approximate report location rather than an exact footprint. Aircraft and vessel positions can be delayed or missing, imagery is not live, and forecasts are not navigation or emergency guidance. Each layer credits its source in the app; the fuller explanations live in [the feed guide](docs/FEEDS.md).

## License

The GlobeView repository carries [AGPL-3.0](LICENSE). Inherited GPL-3.0 code retains its [original license notice](LICENSE-LEGACY-GPL-3.0); MapLibre's license is included [here](maplibre-LICENSE.txt).
