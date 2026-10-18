# PlanIT Control

A responsive operations dashboard built for the supplied PlanIT FastAPI backend. Warm cream surfaces, forest-green navigation, amber alerts, a geographic site map, and focused workspaces for conditions, crew observations, and briefings.

## Quick start

1. Install Node.js 22 or newer if it is not already installed.
2. Extract `PlanIT-Frontend` beside your existing `Backend` folder.
3. Open a terminal in `PlanIT-Frontend` and run:

   ```sh
   npm ci
   npm run dev
   ```

4. Open **http://localhost:5173**.

On Windows, `start-windows.cmd` runs the same steps. On macOS, Linux, or WSL, run `bash start.sh`. Keep the terminal open while using the app. If switching between Windows and WSL, run `npm ci` again in the environment you will use.

If `VITE_API_BASE_URL` is configured at build time, the app connects automatically on startup and loads saved backend data. It waits up to 90 seconds for a sleeping backend, then offers **Retry connection**. Connection errors never open demo data automatically. Without a configured URL, the app opens connection settings. Select **Explore demo** to enter the illustrative Florida workday. Demo edits last only for the current page session.

## Connect your backend

For a hosted backend, set `VITE_API_BASE_URL` to its public origin (for example `https://your-service.onrender.com`) in the frontend build environment. Set it before `npm run build`; Vite embeds this public URL into the build. The backend must allow the frontend origin in CORS. Do not put `PLANIT_WRITE_TOKEN` or provider credentials in any `VITE_` variable.

For local development, start the Python backend on **port 8000** using its existing deployment instructions. Leave **Backend URL** blank in connection settings. The Vite development proxy forwards `/api` to `http://127.0.0.1:8000`. You can also enter another full `http://` or `https://` URL manually.

For the current Render backend, set `VITE_API_BASE_URL=https://swamphacks-xii.onrender.com` in the frontend hosting provider's build environment. Local development continues to use the Vite proxy when no URL is configured.

If your backend has `PLANIT_WRITE_TOKEN` configured, enter that value in the optional **Write token** field. The token stays in this tab's memory and is sent with write requests. It is not saved in local storage. Provider keys such as Xweather or Weather Company credentials stay in the backend's `.env`; this frontend does not need those keys.

Once connected, the app displays sites and conditions from API responses. Live site/report changes are saved by your backend. Reopening the page connects again when `VITE_API_BASE_URL` is configured.

Browser mixed-content rules still apply to manually entered URLs.

## What works

- Ranked, searchable site map with selectable pins, geographic coordinates, map layers, a 10-mile distance reference, and crew observation markers.
- Overview and all-sites table using the backend's rule-based ranking, restriction count, watch levels, and missing/stale data states.
- Seven hazard categories with evidence, hourly heat/rainfall forecasts, required actions, and lightning coverage/countdown.
- Persistent in-app alerts with severity, required action, and supporting evidence.
- Crew reports with raw image upload, coordinates, resolution history, and nearby observations within 10 km.
- Tomorrow's briefing previews, saved briefing history, rule outlook, required actions, observations, data limitations, and JSON export.
- River exposure and HEFS context, including separate period and individual-time bounds with issuance/target times.
- Separate experimental six-hour severe thunderstorm warning prediction, METAR input freshness, generic ML availability, and source freshness details.
- Site-specific resumable SSE events, 30-second portfolio polling, 15-second selected-site polling, and three-second polling while a new site's assessment initializes.
- Responsive desktop, tablet, and phone layouts; keyboard-accessible controls and modal focus management.

## Data behavior

- Flooding displays a **rule-based level** and rainfall evidence. A rainfall value is not a flood probability.
- HEFS numbers retain their meaning as bounds on river-threshold exceedance. They are not jobsite inundation probabilities.
- Unknown or incomplete inputs remain visibly unavailable. Missing model scores remain null; a genuine zero remains zero.
- A lightning timer reaching zero does not create an all-clear. Clearance requires the backend's clear status and fresh, complete measured coverage. The live countdown uses the backend's clock offset.
- Refreshing the view reads saved state. **Refresh assessment** explicitly requests `/risk`. Briefing previews do not consume the backend's scheduled 6 PM delivery.
- Saving a live site immediately queues its first assessment. Pending/running sites show “Assessing…”. A failed initial attempt shows its error and a retry button; completed assessments can still have individual failed sources.
- The six-hour weather-model probability refers only to a new NWS Severe Thunderstorm Warning covering the site. It is experimental, with calibration unestablished. Stale METAR inputs and expired or unavailable predictions are shown explicitly. It never changes rule-based site priority or required actions.
- In-app alerts and SSE do not send email, SMS, or closed-browser push notifications.
- Maps are jobsite location maps; no radar imagery or flood-inundation overlay is fabricated. The radius is a distance reference.
- If the backend disconnects, a visible error marks retained values as potentially stale. The app does not substitute demo data into a connected workspace.

## Standalone preview and production build

`PlanIT-Preview.html`, supplied separately, is a self-contained interactive demo. Download and open it in a browser. The vector land basemap works without a network; street tiles and web fonts use an internet connection. A standalone file has no API proxy, so use the React development project for the simplest live connection.

```sh
npm run build           # Compile the frontend into dist/
npm run preview         # Serve the compiled frontend locally
npm run preview:single  # Regenerate PlanIT-Preview.html
npm test               # Run model and DOM interaction checks
```

`npm run preview` serves the compiled UI, not the development API proxy. Configure `VITE_API_BASE_URL` before building, enter a reachable backend URL manually, or add a reverse proxy before deployment.

## Verification and scope

The production build and automated checks cover the supplied backend response fixtures, missing/stale model data, lightning expiry, HEFS bounds, site creation, report resolution, live-mode switching, and briefing rendering. The API integration tests use mocked transport with the supplied response shapes; they do not claim a connection to your machine's running backend or paid providers. Browser visual QA was unavailable in the build environment.

The frontend uses the backend's saved assessment and weather-model fields. It cannot itself repair the SQLite database, grant lightning-provider access, train a model, or verify jobsite river exposure.

## Project layout

```text
src/App.jsx        Pages, forms, live polling, and SSE
src/Map.jsx        Leaflet map and offline land geometry
src/ui.jsx         Shared controls, forecast chart, and hazard cards
src/model.mjs     Backend response normalization and display semantics
src/api.mjs       API URL, request, timeout, and write-token handling
src/demo.mjs      Explicitly illustrative demo data
src/styles.css    Responsive design system
tests/            Behavioral checks and supplied response fixtures
public/           Offline map geometry and favicon
```

## Credits

Built with React, Vite, Leaflet, and Lucide. Online map tiles: CARTO with OpenStreetMap data, attributed in the map. Offline geography: Natural Earth via world-atlas (public-domain geographic data). Typography: DM Sans and Manrope from Google Fonts, with system fallbacks. This package contains no backend databases or provider credentials.
