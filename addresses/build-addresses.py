#!/usr/bin/env python3
"""Build an offline address pack (SQLite) for one US state from Overture Maps.

    tools/build-addresses.py --state TN --out data/tn/addresses.sqlite
    tools/build-addresses.py --state KY --bbox -89.6,36.4,-81.9,39.2 --out data/ky/addresses.sqlite
    tools/build-addresses.py --state TN --county "Cumberland County" \
        --region-name "Cumberland County, TN" --out data/cumberland/addresses.sqlite
    tools/build-addresses.py --selftest

Requires Python 3 + duckdb (pip install duckdb) for the build; --selftest needs
only the standard library. See docs/address-pack.md for schema, normalization
and the intended query flow.

Stages (each is atomic, so a re-run resumes after the last finished stage):
  1. fetch:  one streaming DuckDB query against the anonymous Overture S3
             bucket -> <out dir>/overture-<STATE>-<release>.parquet (cache;
             reused unless --refetch).
  2. build:  normalize + group in DuckDB/Python -> <out>.part SQLite file,
             indexes, ANALYZE, VACUUM, then rename to <out>.
"""

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from addrnorm import normalize_street, normalize_number, split_house_number  # noqa: E402

PACK_FORMAT = 1
DEFAULT_RELEASE = "2026-10-21.0"
S3_PATH = "s3://overturemaps-us-west-2/release/{release}/theme=addresses/type=address/*"

# State bounding boxes (xmin, ymin, xmax, ymax), slightly padded. Used only for
# Parquet row-group pruning; the real filter is address_levels[1] = state.
STATE_BBOX = {
    "TN": (-90.34, 34.96, -81.62, 36.70),
}

ATTRIBUTION = (
    "Address data: Overture Maps Foundation (overturemaps.org), addresses theme, "
    "release {release}, derived from the National Address Database (NAD) from "
    "the U.S. Department of Transportation and the individual states that "
    "contribute to the database."
)
LICENSE_NOTE = (
    "Source: the US National Address Database (NAD), via the Overture addresses theme "
    "(per-record sources[].license for these rows: {licenses}). The NAD is a work of the "
    "US federal government and so is not subject to copyright protection (17 U.S.C. 105); "
    "it is available for re-use without limitation or restriction."
)

# Words kept upper case when prettifying ALL-CAPS NAD names for display.
KEEP_UPPER = {"US", "SR", "TN", "CR", "NE", "NW", "SE", "SW", "N", "S", "E", "W",
              "I", "II", "III", "IV", "PVT", "MLK"}


def log(msg, t0=[time.time()]):
    print(f"[{time.time() - t0[0]:7.1f}s] {msg}", flush=True)


def pretty_word(w):
    if not w.isupper() or w in KEEP_UPPER:
        return w
    if w[:1].isdigit():
        return w.lower()  # 12TH -> 12th, 19E -> 19e
    out, cap = [], True
    for c in w:
        out.append(c.upper() if cap else c.lower())
        cap = not c.isalnum()  # O'NEIL -> O'Neil, SMITH-JONES -> Smith-Jones
    return "".join(out)


def pretty(s):
    return " ".join(pretty_word(w) for w in (s or "").split())


# ---------------------------------------------------------------- self-test

def selftest():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "address-normalize-cases.json"), encoding="utf-8") as f:
        cases = json.load(f)
    bad = 0
    for raw, want in [("8681", "8681"), ("12-B", "12b"), ("12 1/2", "121/2"), ("Rear", "rear")]:
        if normalize_number(raw) != want:
            bad += 1
            print(f"FAIL normalize_number({raw!r}) = {normalize_number(raw)!r}, want {want!r}")
    for raw, unit, want in [("8681", "", ("8681", 8681, "")), ("509 A", "", ("509", 509, "a")),
                            ("112B", "", ("112", 112, "b")), ("101 1/2", "", ("101", 101, "1/2")),
                            ("225 LOT", "LOT 13", ("225", 225, "13")),
                            ("509 B", "102", ("509", 509, "b102")), ("12", "Apt. 4-B", ("12", 12, "4b"))]:
        if split_house_number(raw, unit) != want:
            bad += 1
            print(f"FAIL split_house_number({raw!r}, {unit!r}) = "
                  f"{split_house_number(raw, unit)!r}, want {want!r}")
    for c in cases:
        got = normalize_street(c["input"])
        if got != c["norm"]:
            bad += 1
            print(f"FAIL {c['input']!r}: got {got!r}, want {c['norm']!r}")
    print(f"{len(cases) - bad}/{len(cases)} normalization cases pass")
    return 0 if bad == 0 else 1


# ---------------------------------------------------------------- stage 1

def fetch(con, state, release, bbox, cache):
    if os.path.exists(cache):
        log(f"fetch: reusing cached {cache}")
        return
    xmin, ymin, xmax, ymax = bbox
    part = cache + ".part"
    log(f"fetch: Overture {release} addresses, state={state}, bbox={bbox} -> {cache}")
    con.execute(f"""
        COPY (
          SELECT number, street, unit, postcode, postal_city,
                 [l.value FOR l IN address_levels] AS levels,
                 ST_X(geometry) AS x, ST_Y(geometry) AS y,
                 sources[1].dataset AS dataset, sources[1].license AS license,
                 sources[1].update_time AS update_time
          FROM read_parquet('{S3_PATH.format(release=release)}')
          WHERE bbox.xmin BETWEEN {xmin} AND {xmax}
            AND bbox.ymin BETWEEN {ymin} AND {ymax}
            AND country = 'US' AND address_levels[1].value = '{state}'
        ) TO '{part}' (FORMAT parquet, COMPRESSION zstd)""")
    os.replace(part, cache)
    log("fetch: done")


# ---------------------------------------------------------------- region

DIVISIONS_PATH = ("s3://overturemaps-us-west-2/release/{release}/"
                  "theme=divisions/type=division_area/*")
STATE_NAMES = {"TN": "Tennessee"}


def county_polygon(con, state, county, release, bbox, cache_dir):
    """WKT of the county's land polygon from Overture divisions (cached)."""
    slug = "".join(c if c.isalnum() else "-" for c in county.lower()).strip("-")
    path = os.path.join(cache_dir, f"overture-county-{state}-{slug}-{release}.wkt")
    if os.path.exists(path):
        log(f"region: reusing cached boundary {path}")
        with open(path, encoding="utf-8") as f:
            div_id, wkt = f.read().split("\n", 1)
        return div_id, wkt
    xmin, ymin, xmax, ymax = bbox
    log(f"region: fetching '{county}' boundary (Overture divisions, region US-{state})")
    rows = con.execute(f"""
        SELECT division_id, names.primary, ST_AsText(geometry)
        FROM read_parquet('{DIVISIONS_PATH.format(release=release)}')
        WHERE bbox.xmin >= {xmin} AND bbox.xmax <= {xmax}
          AND bbox.ymin >= {ymin} AND bbox.ymax <= {ymax}
          AND subtype = 'county' AND class = 'land' AND country = 'US'
          AND region = 'US-{state}' AND lower(names.primary) = lower(?)""",
                       [county]).fetchall()
    if len(rows) != 1:
        raise SystemExit(f"expected exactly one county named {county!r} in US-{state}, "
                         f"found {len(rows)}: {[r[:2] for r in rows]}")
    div_id, _, wkt = rows[0]
    with open(path + ".part", "w", encoding="utf-8") as f:
        f.write(div_id + "\n" + wkt)
    os.replace(path + ".part", path)
    return div_id, wkt


def state_bbox(con, state, release):
    """Padded bounding box of a state (or DC) from Overture divisions, for Parquet row-group pruning."""
    row = con.execute(f"""
        SELECT min(bbox.xmin), min(bbox.ymin), max(bbox.xmax), max(bbox.ymax)
        FROM read_parquet('{DIVISIONS_PATH.format(release=release)}')
        WHERE country = 'US' AND region = 'US-{state}' AND class = 'land'
          AND subtype IN ('region', 'county')""").fetchone()
    if row is None or row[0] is None:
        raise SystemExit(f"no Overture division found for US-{state}; pass --bbox")
    box = (round(row[0] - 0.05, 3), round(row[1] - 0.05, 3), round(row[2] + 0.05, 3), round(row[3] + 0.05, 3))
    log(f"bbox for {state} from Overture divisions: {box}")
    return box


MIN_ZIP_ADDRESSES = 25  # smaller "ZIPs" in NAD are typos/strays ('3855', '39558')


def select_region(con, cache, state, release, bbox, county, zips, threshold, name, cache_dir):
    """Pick the rows of a regional pack.

    With --county, the pack holds
      * every address inside the county polygon (whatever its ZIP), plus
      * ALL addresses of each ZIP with >= threshold of its statewide addresses
        inside the county, so a ZIP straddling the county line stays whole.
    A "ZIP" must be 5 digits with >= MIN_ZIP_ADDRESSES statewide addresses to
    be included whole; smaller ones are data-entry strays (their in-county
    addresses are still covered by the polygon)."""
    src = f"read_parquet('{cache}')"
    region = {"name": name, "threshold": "", "zips": []}
    if zips:
        region["type"] = "zips"
        region["zips"] = sorted(set(zips))
        region.pop("threshold")
    if county:
        div_id, wkt = county_polygon(con, state, county, release, bbox, cache_dir)
        region.update({"type": "county", "county": county,
                       "boundary": f"Overture divisions {release}, division_id {div_id}",
                       "threshold": str(threshold)})
        rows = con.execute(f"""
            WITH c AS (SELECT ST_GeomFromText(?) AS g),
            s AS (SELECT coalesce(postcode, '') AS zip,
                         ST_Contains(c.g, ST_Point(x, y)) AS inside
                  FROM {src}, c)
            SELECT zip, count(*) AS total, count(*) FILTER (WHERE inside) AS n_in
            FROM s GROUP BY zip HAVING n_in > 0 ORDER BY n_in DESC""", [wkt]).fetchall()
        total_in = sum(r[2] for r in rows)
        log(f"region: {total_in:,} addresses inside {county}, {state}")
        print(f"   {'ZIP':<6} {'in county':>10} {'ZIP total':>10} {'share':>7}  decision")
        chosen, near_miss = [], []
        for z, tot, n_in in rows:
            share = n_in / tot
            real = len(z) == 5 and z.isdigit() and tot >= MIN_ZIP_ADDRESSES
            keep = real and share >= threshold
            if keep:
                chosen.append(z)
                why = "include whole ZIP"
            elif not real:
                why = "stray/invalid ZIP: in-county rows only"
            else:
                why = "in-county rows only"
                near_miss.append((z, share))
            print(f"   {z or '(none)':<6} {n_in:>10,} {tot:>10,} {share:>6.1%}  {why}")
        region["zips"] = sorted(set(region["zips"]) | set(chosen))
        kept_in = sum(r[2] for r in rows if r[0] in region["zips"])
        log(f"region: ZIPs included whole: {', '.join(region['zips'])}")
        if near_miss:
            log("region: ZIPs below --zip-threshold (only their in-county addresses are kept): "
                + ", ".join(f"{z} {sh:.1%}" for z, sh in near_miss))
        log(f"region: in-county addresses inside the whole ZIPs: {kept_in:,}/{total_in:,} "
            f"({kept_in / max(total_in, 1):.1%}); the rest are added by the polygon")
        region["inside_county"] = str(total_in)
        region["partial_zips"] = [z for z, _ in near_miss]
        region["_wkt"] = wkt
    if not region["zips"]:
        raise SystemExit("region selection produced no ZIPs")
    return region


# ---------------------------------------------------------------- stage 2

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE streets(
  id       INTEGER PRIMARY KEY,
  name     TEXT NOT NULL,      -- display name, e.g. 'Highway 70 East'
  norm     TEXT NOT NULL,      -- normalize_street(name), e.g. 'hwy 70 e'
  city     TEXT,               -- municipality (address_levels[2]) or postal city
  pcity    TEXT,               -- USPS postal city, e.g. 'Crossville'
  postcode TEXT,
  lat      INTEGER NOT NULL,   -- centroid, microdegrees
  lon      INTEGER NOT NULL
);
CREATE TABLE addr(
  street_id INTEGER NOT NULL,
  num       INTEGER NOT NULL,  -- leading digits of number, -1 if none
  number    TEXT NOT NULL,     -- split_house_number(): '8681' (letter/fraction suffixes go to unit)
  unit      TEXT NOT NULL,     -- normalize_unit(): '' when none, 'a', '1/2', '4b'
  lat       INTEGER NOT NULL,
  lon       INTEGER NOT NULL,
  PRIMARY KEY(street_id, num, number, unit)
) WITHOUT ROWID;
"""


def build(con, cache, out, state, release, bbox, region=None):
    """region: None for a whole-state pack, else a dict from select_region()
    whose 'zips' limits the rows and whose other keys go into meta."""
    part = out + ".part"
    if os.path.exists(part):
        os.remove(part)
    t_build = time.time()
    where = ""
    if region:
        # whole chosen ZIPs, plus (with --county) every address inside the
        # county polygon whatever its ZIP says
        where = "WHERE postcode IN (" + ",".join(f"'{z}'" for z in region["zips"]) + ")"
        if region.get("_wkt"):
            con.execute("CREATE OR REPLACE TEMP TABLE county_poly AS "
                        "SELECT ST_GeomFromText(?) AS g", [region["_wkt"]])
            where += (" OR ST_Contains((SELECT g FROM county_poly), ST_Point(x, y))")
    con.execute(f"CREATE OR REPLACE TEMP TABLE src AS SELECT * FROM read_parquet('{cache}') {where}")
    src = "src"
    if region:  # bbox of what is actually in the pack
        b = con.execute("SELECT min(x), min(y), max(x), max(y) FROM src").fetchone()
        bbox = tuple(round(v, 5) for v in b)
    total = con.execute(f"SELECT count(*) FROM {src}").fetchone()[0]
    log(f"build: {total:,} source rows")

    # 1. normalize each distinct street name once (Python), hand back to DuckDB.
    names = [r[0] for r in con.execute(
        f"SELECT DISTINCT street FROM {src} WHERE street IS NOT NULL").fetchall()]
    log(f"build: normalizing {len(names):,} distinct street names")
    rows = [(n, normalize_street(n), pretty(n)) for n in names]
    con.execute("CREATE TEMP TABLE sn(street VARCHAR, norm VARCHAR, name VARCHAR)")
    # (bulk load through a temp TSV: far faster than executemany into DuckDB)
    tmp = out + ".names.tsv"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write("\t".join(x.replace("\t", " ") for x in r) + "\n")
    con.execute(f"INSERT INTO sn SELECT * FROM read_csv('{tmp}', delim='\t', "
                "header=false, quote='', escape='', "
                "columns={'street':'VARCHAR','norm':'VARCHAR','name':'VARCHAR'})")
    os.unlink(tmp)
    empty = sum(1 for r in rows if not r[1])
    log(f"build: {empty} names normalize to '' (dropped)")

    # 2. joined, cleaned rows.
    con.execute(f"""
      CREATE TEMP TABLE a AS
      SELECT sn.norm, sn.name,
             coalesce(nullif(trim(s.levels[2]), ''), s.postal_city) AS city_raw,
             s.postal_city AS pcity_raw,
             coalesce(s.postcode, '') AS postcode,
             s.number, coalesce(s.unit, '') AS unit_raw, s.x, s.y
      FROM {src} s JOIN sn ON s.street = sn.street
      WHERE sn.norm <> '' AND s.number IS NOT NULL AND s.x IS NOT NULL""")

    # 3. street groups: one row per (norm, city, postcode); display name and
    #    postal city = most common spelling; centroid = mean point.
    con.execute("""
      CREATE TEMP TABLE g AS
      SELECT row_number() OVER (ORDER BY norm, upper(city_raw), postcode) AS id,
             norm, upper(city_raw) AS cityk, postcode,
             mode(name) AS name, mode(city_raw) AS city, mode(pcity_raw) AS pcity,
             avg(y) AS lat, avg(x) AS lon
      FROM a GROUP BY norm, upper(city_raw), postcode""")
    nstreets = con.execute("SELECT count(*) FROM g").fetchone()[0]
    log(f"build: {nstreets:,} street groups")

    db = sqlite3.connect(part)
    db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; "
                     "PRAGMA page_size=4096; PRAGMA cache_size=-400000;")
    db.executescript(SCHEMA)

    cur = con.execute("SELECT id, name, norm, city, pcity, postcode, lat, lon FROM g ORDER BY id")
    while True:
        batch = cur.fetchmany(50000)
        if not batch:
            break
        db.executemany("INSERT INTO streets VALUES (?,?,?,?,?,?,?,?)", [
            (i, nm, no, pretty(c), pretty(pc), pz or None,
             round(la * 1e6), round(lo * 1e6))
            for i, nm, no, c, pc, pz, la, lo in batch])
    log("build: streets written")

    # 4. address rows. House number / unit keys come from
    #    addrnorm.split_house_number(), run once per distinct (number, unit)
    #    pair; rows are deduplicated on the primary key (first point wins).
    pairs = con.execute(f"SELECT DISTINCT number, coalesce(unit, '') FROM {src} "
                        "WHERE number IS NOT NULL").fetchall()
    log(f"build: splitting {len(pairs):,} distinct (number, unit) pairs")
    tmp = out + ".nums.tsv"
    with open(tmp, "w", encoding="utf-8") as f:
        for number, unit in pairs:
            nn, num, un = split_house_number(number, unit)
            f.write("\t".join((number.replace("\t", " "), unit.replace("\t", " "),
                                nn, str(num), un)) + "\n")
    con.execute(f"CREATE TEMP TABLE nu AS SELECT * FROM read_csv('{tmp}', delim='\t', "
                "header=false, quote='', escape='', columns={'number_raw':'VARCHAR',"
                "'unit_raw':'VARCHAR','number':'VARCHAR','num':'BIGINT','unit':'VARCHAR'})")
    os.unlink(tmp)
    con.execute("UPDATE nu SET unit_raw = coalesce(unit_raw, ''), unit = coalesce(unit, ''), "
                "number = coalesce(number, '')")
    con.execute("""
      CREATE TEMP TABLE ad AS
      SELECT g.id AS street_id, nu.num, nu.number, nu.unit,
             CAST(round(a.y * 1e6) AS BIGINT) AS lat, CAST(round(a.x * 1e6) AS BIGINT) AS lon
      FROM a
      JOIN g ON a.norm = g.norm AND upper(a.city_raw) IS NOT DISTINCT FROM g.cityk
            AND a.postcode = g.postcode
      JOIN nu ON a.number = nu.number_raw AND a.unit_raw = nu.unit_raw
      WHERE nu.number <> ''
      QUALIFY row_number() OVER (PARTITION BY g.id, nu.num, nu.number, nu.unit
                                 ORDER BY lat, lon) = 1""")
    naddr = con.execute("SELECT count(*) FROM ad").fetchone()[0]
    cur = con.execute("SELECT * FROM ad ORDER BY street_id, num, number, unit")
    done = 0
    while True:
        batch = cur.fetchmany(250000)
        if not batch:
            break
        db.executemany("INSERT INTO addr VALUES (?,?,?,?,?,?)", batch)
        done += len(batch)
        log(f"build: addr {done:,}/{naddr:,}")
    log(f"build: {naddr:,} address rows written ({total - naddr:,} duplicates/invalid dropped)")

    db.execute("CREATE INDEX streets_norm ON streets(norm)")
    db.execute("CREATE INDEX streets_postcode ON streets(postcode)")

    lic = [r[0] for r in con.execute(
        f"SELECT DISTINCT coalesce(license, 'null') FROM {src} ORDER BY 1").fetchall()]
    ds = [r[0] for r in con.execute(
        f"SELECT DISTINCT coalesce(dataset, 'null') FROM {src} ORDER BY 1").fetchall()]
    upd = con.execute(f"SELECT min(update_time), max(update_time) FROM {src}").fetchone()
    meta = {
        "format": str(PACK_FORMAT),
        "source": "Overture addresses",
        "release": release,
        "datasets": ",".join(ds),
        "source_update_time": f"{upd[0]}..{upd[1]}",
        "state": state,
        "country": "US",
        "built_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_rows": str(total),
        "streets": str(nstreets),
        "addresses": str(naddr),
        "bbox": ",".join(str(v) for v in bbox),
        "region_name": region["name"] if region else STATE_NAMES.get(state, state),
        "coord_unit": "microdegrees (value * 1e6, WGS84)",
        "normalizer": "docs/address-pack.md#normalization v1",
        "attribution": ATTRIBUTION.format(release=release),
        "license": LICENSE_NOTE.format(licenses=", ".join(lic)),
    }
    if region:
        for k, v in region.items():
            if k != "name" and not k.startswith("_"):
                meta["region_" + k] = ",".join(v) if isinstance(v, list) else str(v)
    db.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
    db.commit()
    log("build: ANALYZE")
    db.execute("ANALYZE")
    db.execute(f"PRAGMA user_version = {PACK_FORMAT}")
    db.commit()
    log("build: VACUUM")
    db.execute("VACUUM")
    db.execute("PRAGMA journal_mode=DELETE")
    db.close()
    os.replace(part, out)
    log(f"build: wrote {out} ({os.path.getsize(out) / 1e6:.1f} MB) "
        f"in {time.time() - t_build:.0f}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="two-letter state code, e.g. TN")
    ap.add_argument("--out", help="output SQLite path, e.g. data/tn/addresses.sqlite")
    ap.add_argument("--release", default=DEFAULT_RELEASE)
    ap.add_argument("--bbox", help="xmin,ymin,xmax,ymax (required for states without a built-in bbox)")
    ap.add_argument("--refetch", action="store_true", help="ignore the cached Overture extract")
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--selftest", action="store_true", help="check normalization vectors and exit")
    g = ap.add_argument_group("regional pack (subset of one state)")
    g.add_argument("--county", help="county name as in Overture divisions, e.g. 'Cumberland County'")
    g.add_argument("--zips", help="comma-separated ZIP codes to include (alone or added to --county)")
    g.add_argument("--zip-threshold", type=float, default=0.25,
                   help="with --county: include a ZIP when at least this share of its statewide "
                        "addresses is inside the county (default 0.25)")
    g.add_argument("--region-name", help="stored in meta.region_name, e.g. 'Cumberland County, TN'")
    g.add_argument("--cache", help="statewide Overture Parquet cache (default: data/<state>/ "
                                   "next to the output for state packs, the sibling "
                                   "<data>/<state>/ directory for regional packs)")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.state or not a.out:
        ap.error("--state and --out are required")
    state = a.state.upper()
    if a.bbox:
        bbox = tuple(float(v) for v in a.bbox.split(","))
        if len(bbox) != 4:
            ap.error("--bbox needs 4 numbers")
    elif state in STATE_BBOX:
        bbox = STATE_BBOX[state]
    else:
        bbox = None  # looked up from Overture divisions once the connection is open
    if selftest() != 0:
        print("normalization self-test failed; refusing to build", file=sys.stderr)
        return 1

    import duckdb  # only needed for the build
    out = os.path.abspath(a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    regional = bool(a.county or a.zips)
    cache_dir = os.path.dirname(out)
    if regional:  # share the statewide cache, e.g. data/tn/ for data/cumberland/x.sqlite
        cache_dir = os.path.join(os.path.dirname(cache_dir), state.lower())
        os.makedirs(cache_dir, exist_ok=True)
    cache = a.cache or os.path.join(cache_dir, f"overture-{state}-{a.release}.parquet")
    if a.refetch and os.path.exists(cache):
        os.remove(cache)
    con = duckdb.connect()
    con.execute(f"SET threads={a.threads}")
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; "
                "SET s3_region='us-west-2';")
    if bbox is None:
        bbox = state_bbox(con, state, a.release)
    fetch(con, state, a.release, bbox, cache)
    region = None
    if regional:
        zips = [z.strip() for z in a.zips.split(",") if z.strip()] if a.zips else []
        name = a.region_name or (f"{a.county}, {state}" if a.county else f"ZIPs {','.join(zips)}")
        region = select_region(con, cache, state, a.release, bbox, a.county, zips,
                               a.zip_threshold, name, os.path.dirname(cache))
    build(con, cache, out, state, a.release, bbox, region)
    return 0


if __name__ == "__main__":
    sys.exit(main())
