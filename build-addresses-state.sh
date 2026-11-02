#!/usr/bin/env bash
# Build one US state's offline address pack and package it for the Route Planner app.
#   ./build-addresses-state.sh tennessee [out-dir]
# Writes out-dir/<state>.sqlite.gz and out-dir/<state>.addresses.json (sizes, SHA-256, Overture release).
# Needs python3 with duckdb (pip install duckdb). Data: Overture Maps addresses theme (US National Address Database, public domain).
set -euo pipefail
state=${1:?usage: build-addresses-state.sh <geofabrik-state-name> [out-dir]}
out=$(mkdir -p "${2:-out}" && realpath "${2:-out}")
here=$(cd "$(dirname "$0")" && pwd)
IFS=$'\t' read -r _ code name < <(grep -P "^$state\t" "$here/states/codes.tsv" || true)
[ -n "${code:-}" ] || { echo "unknown state $state" >&2; exit 1; }
work="$out/work-$state"; mkdir -p "$work"
python3 "$here/addresses/build-addresses.py" --state "$code" --region-name "$name" --out "$work/addresses.sqlite" ${OVERTURE_RELEASE:+--release "$OVERTURE_RELEASE"}
release=$(python3 - "$work/addresses.sqlite" <<'PY'
import sqlite3, sys
print(dict(sqlite3.connect(sys.argv[1]).execute("select key,value from meta").fetchall()).get("release", ""))
PY
)
mv "$work/addresses.sqlite" "$out/$state.sqlite"
gzip -9 -c "$out/$state.sqlite" > "$out/$state.sqlite.gz"
python3 - "$state" "$code" "$name" "$release" "$out/$state.sqlite" "$out/$state.sqlite.gz" > "$out/$state.addresses.json" <<'PY'
import sys, json, hashlib, os, datetime
state, code, name, release, raw, gz = sys.argv[1:]
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 24), b""): h.update(c)
    return h.hexdigest()
print(json.dumps({"state": state, "code": code, "name": name, "release": release, "built": datetime.date.today().isoformat(),
                  "sqlite_bytes": os.path.getsize(raw), "sqlite_sha256": sha(raw),
                  "file": os.path.basename(gz), "gz_bytes": os.path.getsize(gz), "gz_sha256": sha(gz)}, indent=1))
PY
rm -rf "$work" "$out/$state.sqlite"
ls -lh "$out/$state".*
