#!/usr/bin/env bash
# Build one US state's Valhalla road tiles and package them for the Route Planner app.
#   ./build-state.sh tennessee [out-dir]
# Writes out-dir/<state>.tar.gz.NNN (parts of at most 1900 MB, because a release file may not exceed 2 GB)
# and out-dir/<state>.json (sizes, checksums, source date, Valhalla version).
# The app joins the parts, gunzips them and gets valhalla_tiles.tar. Needs podman or docker.
set -euo pipefail
state=${1:?usage: build-state.sh <geofabrik-us-state> [out-dir]}
out=$(mkdir -p "${2:-out}" && realpath "${2:-out}")
img=${VALHALLA_IMAGE:-ghcr.io/valhalla/valhalla:3.9.1}   # keep equal to the engine inside valhalla-mobile
rt=${CONTAINER_RT:-$(command -v podman || command -v docker)}
ua="route-planner-roads (+https://github.com/Craterbrain/route-planner-roads; builds each state at most a few times a year)"
url="https://download.geofabrik.de/north-america/us/$state-latest.osm.pbf"
work="$out/work-$state"; mkdir -p "$work"

# One download per state, rate-limited, retried politely.
curl -fsSL --retry 3 --retry-delay 120 --limit-rate "${RATE:-20M}" -A "$ua" -D "$work/headers" -o "$work/region.osm.pbf" "$url"
modified=$(grep -i '^last-modified:' "$work/headers" | tail -1 | cut -d' ' -f2- | tr -d '\r')

"$rt" run --rm -v "$work:/data" "$img" bash -c '
  set -e
  valhalla_build_config --mjolnir-tile-dir /data/tiles --mjolnir-tile-extract /data/valhalla_tiles.tar \
    --mjolnir-traffic-extract /data/traffic.tar > /data/valhalla.json
  valhalla_build_tiles -c /data/valhalla.json /data/region.osm.pbf
  valhalla_build_extract -c /data/valhalla.json -v
'
rm -rf "$work/tiles" "$work/region.osm.pbf"
tar_bytes=$(stat -c %s "$work/valhalla_tiles.tar")
tar_sha=$(sha256sum "$work/valhalla_tiles.tar" | cut -d' ' -f1)
gzip -6 -c "$work/valhalla_tiles.tar" | split -d -a 3 -b 1900M - "$out/$state.tar.gz."
rm -rf "$work"

parts=$(cd "$out" && ls "$state.tar.gz."* | python3 -c '
import sys,hashlib,os,json
r=[]
for n in sys.stdin.read().split():
    h=hashlib.sha256(open(n,"rb").read() if os.path.getsize(n)<(1<<28) else b"")
    if os.path.getsize(n)>=(1<<28):
        h=hashlib.sha256()
        with open(n,"rb") as f:
            for c in iter(lambda:f.read(1<<24),b""): h.update(c)
    r.append({"name":n,"bytes":os.path.getsize(n),"sha256":h.hexdigest()})
print(json.dumps(r))')
python3 - "$state" "$modified" "$tar_bytes" "$tar_sha" "$img" "$parts" > "$out/$state.json" <<'PY'
import sys, json, datetime
state, modified, tb, ts, img, parts = sys.argv[1:]
print(json.dumps({"state": state, "source_last_modified": modified, "built": datetime.date.today().isoformat(),
                  "valhalla": img.rsplit(":", 1)[-1], "tar_bytes": int(tb), "tar_sha256": ts, "parts": json.loads(parts)}, indent=1))
PY
ls -lh "$out/$state".*
