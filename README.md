# route-planner-roads

Road-network data (Valhalla tiles) for each US state, used by the Route Planner Android app for on-device routing.

- Built from [Geofabrik](https://download.geofabrik.de/) OpenStreetMap extracts (ODbL, © OpenStreetMap contributors) with Valhalla 3.9.1 (the version inside the app's `valhalla-mobile`).
- Published as the `roads` release: `<state>.tar.gz.000…` (parts of ≤1900 MB, because a release file may not exceed 2 GB), `<state>.json` and `manifest.json` (sizes, SHA-256, source date).
- Manual builds only (Actions → *Build road data*): `group-1`, `group-2`, `extra` (DC). One Geofabrik download per state, rate-limited, two at a time, skipped when Geofabrik's file hasn't changed. Rebuild a few times a year, not more often.
- Locally: `./build-state.sh tennessee`.
