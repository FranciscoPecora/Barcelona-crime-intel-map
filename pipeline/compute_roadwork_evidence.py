#!/usr/bin/env python3
"""
Providence — Historical Roadwork→Congestion Evidence Pipeline
=============================================================

WHAT THIS DOES
--------------
For each Barcelona traffic section, over a rolling 12-month window, this computes
a natural experiment:

    How often is this section congested WHEN a roadwork is active on it,
    versus WHEN no roadwork is active?

If a section is congested far more often during roadworks than otherwise, that is
real temporal evidence (not a single snapshot) that roadworks drive congestion
*there specifically*. The output is a small per-section summary that Providence
loads and displays — the heavy history is distilled here, never in the browser.

WHY IT'S HONEST
---------------
- It reports the NUMBER OF OBSERVATIONS behind every statistic, so weak evidence
  (few observations) is visible, not hidden.
- It computes a "lift" (congestion rate with roadwork / rate without) but labels
  low-observation sections as "insufficient evidence" rather than overclaiming.
- Correlation is still not causation: a co-located roadwork and congestion may
  share a common cause (a busy street gets both). The output says so.

ARCHITECTURE
------------
This runs OUTSIDE the browser — via GitHub Actions weekly (see the workflow file).
It downloads only recent monthly traffic archives, joins them against roadwork
date ranges + geometry, and writes `roadwork_evidence.json` (~534 rows, small).
Providence fetches that file like any other data source.

DATA SOURCES
------------
- Traffic history: monthly CSVs on dataset `trams`, format:
      idTram#timestamp(YYYYMMDDHHMMSS)#estatActual(0-6)#estatPrevist(0-6)
- Section geometry: transit_relacio_trams.csv  (idTram -> polyline)
- Roadworks: obres dataset (JSON) with data_inici/data_fi + geometria_wgs84 (WKT)
"""

import csv
import io
import json
import sys
import zipfile
import datetime as dt
from collections import defaultdict
from urllib.request import urlopen, Request

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
WINDOW_MONTHS = 2                      # START SMALL: validate end-to-end on 2
                                       # months first. Once a run succeeds and
                                       # produces evidence, raise to 12 for the
                                       # full rolling window.
CONGESTED_THRESHOLD = 5                # state >= 5 counts as "congested" (5=congestió,6=tallat)
DENSE_THRESHOLD = 3                    # state >= 3 counts as "dense or worse"
MIN_OBSERVATIONS = 200                 # below this, evidence is "insufficient"
CKAN_PACKAGE = "trams"
CKAN_BASE = "https://opendata-ajuntament.barcelona.cat/data/api/3/action"
ROADWORKS_URL = ("https://opendata-ajuntament.barcelona.cat/data/dataset/"
                 "fd9f355f-2160-4f89-96a1-6ece3924e3bd/resource/"
                 "089bcf9e-140e-4ea3-bf93-03c6260ba0f5/download")
GEOM_URL = ("https://opendata-ajuntament.barcelona.cat/data/dataset/"
            "1090983a-1c40-4609-8620-14ad49aae3ab/resource/"
            "1d6c814c-70ef-4147-aa16-a49ddb952f72/download/transit_relacio_trams.csv")
OUTPUT_PATH = "roadwork_evidence.json"

import gzip
from urllib.parse import quote

# Barcelona's open-data portal blocks direct programmatic access (HTTP 403), so
# every request goes through the same Cloudflare Worker proxy the app uses.
WORKER = "https://cicero.franzpec2017.workers.dev"
UA = {
    "User-Agent": "Mozilla/5.0 (compatible; Providence-Pipeline/1.0)",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
}


def _encode_uri_component(s):
    """Match JavaScript's encodeURIComponent exactly. Python's quote() has a
    different default safe set; encodeURIComponent leaves A-Za-z0-9-_.!~*'()
    unescaped and escapes everything else (including / : ? & =)."""
    return quote(s, safe="-_.!~*'()")


def _proxied(url):
    """Wrap a Barcelona-portal URL in the Worker proxy, encoding the URL exactly
    as the app's JS does (encodeURIComponent) so the Worker sees an identical
    request. Format: WORKER?url=<encoded>  (no slash before '?')."""
    if url.startswith(WORKER):
        return url
    return f"{WORKER}?url={_encode_uri_component(url)}"


def fetch(url, timeout=180, use_proxy=True):
    """Fetch raw bytes. Tries direct first, then the Worker proxy on failure.
    Barcelona blocks direct file downloads (403) but the CKAN API sometimes
    allows direct access, so we attempt both and use whichever works."""
    from urllib.request import build_opener, HTTPRedirectHandler
    opener = build_opener(HTTPRedirectHandler())
    attempts = []
    if use_proxy:
        # Proxy works from any IP (residential or cloud); direct only works from
        # non-blocked IPs. Proxy-first is safe everywhere.
        attempts = [_proxied(url), url]
    else:
        attempts = [url]
    last_err = None
    for target in attempts:
        try:
            req = Request(target, headers=UA)
            with opener.open(req, timeout=timeout) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
                if "gzip" in enc:
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        pass
                return raw
        except Exception as e:
            last_err = e
            print(f"    fetch attempt failed [{target[:70]}...]: {e}", file=sys.stderr)
            continue
    raise last_err if last_err else RuntimeError(f"all fetch attempts failed for {url}")


def fetch_text(url, timeout=180):
    raw = fetch(url, timeout)
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_json(url, timeout=180):
    """Fetch and parse JSON, with a clear error if the response isn't JSON."""
    text = fetch_text(url, timeout).strip()
    if not text:
        raise ValueError(f"empty response from {url}")
    if text[:1] not in ("{", "["):
        raise ValueError(f"non-JSON response from {url} (starts with: {text[:60]!r})")
    return json.loads(text)


# ----------------------------------------------------------------------------
# 1. Section geometry: idTram -> representative point (centroid of polyline)
# ----------------------------------------------------------------------------
def load_geometry():
    """Return {section_id: (lat, lng)} representative point per section."""
    text = fetch_text(GEOM_URL)
    if not text or len(text) < 100:
        raise ValueError(f"geometry response too short ({len(text)} chars): {text[:80]!r}")
    geom = {}
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)  # header: Tram,Descripció,Coordenades
    for row in reader:
        if len(row) < 3:
            continue
        sid = row[0].strip()
        # coordinate string is the LAST field (may contain many comma-separated
        # numbers); join everything from index 2 on, in case description had commas
        coord_str = row[2] if len(row) == 3 else ",".join(row[2:])
        coords = [float(x) for x in coord_str.split(",") if x.strip().replace(".", "").replace("-", "").isdigit()]
        if len(coords) < 2:
            continue
        mid = (len(coords) // 2) // 2 * 2
        lng, lat = coords[mid], coords[mid + 1]
        geom[sid] = (lat, lng)
    return geom


# ----------------------------------------------------------------------------
# 2. Roadworks: build per-section active-date intervals
# ----------------------------------------------------------------------------
def parse_wkt_centroid(wkt):
    """Rough centroid of a WKT POLYGON in lng lat pairs -> (lat, lng)."""
    try:
        inner = wkt[wkt.index("((") + 2: wkt.index("))")]
        pts = []
        for pair in inner.split(","):
            lng, lat = pair.strip().split()
            pts.append((float(lat), float(lng)))
        if not pts:
            return None
        return (sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts))
    except Exception:
        return None


def haversine_km(a, b):
    import math
    R = 6371.0
    lat1, lng1, lat2, lng2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def load_roadwork_intervals(geom, radius_km=0.25):
    """
    For each section, list of (start_date, end_date) intervals during which a
    roadwork was active WITHIN radius_km of the section's representative point.
    A section is 'under roadwork' at time T if T falls in any such interval.
    """
    raw_works = None
    try:
        data = fetch_json(ROADWORKS_URL)
        works = data if isinstance(data, list) else data.get("result", [])
    except Exception as e:
        print(f"    roadworks direct fetch failed ({e}); trying CKAN datastore",
              file=sys.stderr)
        # fallback: CKAN datastore_search for the obres resource
        api = (f"{CKAN_BASE}/datastore_search?resource_id="
               "089bcf9e-140e-4ea3-bf93-03c6260ba0f5&limit=5000")
        data = fetch_json(api)
        works = data.get("result", {}).get("records", [])

    # centroids of works with valid dates
    parsed = []
    for w in works:
        c = parse_wkt_centroid(w.get("geometria_wgs84", "") or "")
        if not c:
            continue
        si, sf = w.get("data_inici"), w.get("data_fi")
        if not si or not sf:
            continue
        try:
            d0 = dt.date.fromisoformat(si[:10])
            d1 = dt.date.fromisoformat(sf[:10])
        except ValueError:
            continue
        parsed.append((c, d0, d1))

    # assign each work to nearby sections
    intervals = defaultdict(list)
    for sid, pt in geom.items():
        for (c, d0, d1) in parsed:
            if haversine_km(pt, c) <= radius_km:
                intervals[sid].append((d0, d1))
    return intervals


def under_roadwork(intervals_for_section, date):
    for (d0, d1) in intervals_for_section:
        if d0 <= date <= d1:
            return True
    return False


# ----------------------------------------------------------------------------
# 3. Traffic history: resolve recent monthly CSV resources, stream them
# ----------------------------------------------------------------------------
def resolve_monthly_resources():
    """Return list of (name, url) for monthly traffic CSVs, newest first."""
    api = f"{CKAN_BASE}/package_show?id={CKAN_PACKAGE}"
    data = fetch_json(api)
    resources = data["result"]["resources"]
    monthly = []
    for r in resources:
        name = (r.get("name") or "").upper()
        url = r.get("url") or ""
        # monthly archives look like 2026_01_GENER_TRAMS_TRAMS.csv
        if url.lower().endswith(".csv") and "TRAMS_TRAMS" in name and any(ch.isdigit() for ch in name[:5]):
            monthly.append((r.get("name"), url, r.get("last_modified") or ""))
    monthly.sort(key=lambda x: x[2], reverse=True)
    return [(n, u) for (n, u, _) in monthly]


def accumulate_month(url, intervals, stats):
    """
    Stream one monthly CSV, tally per-section observations split by whether a
    roadwork was active on that date. `stats[sid]` = dict of counters.

    Monthly archive format (CONFIRMED against a real file) is CSV with a header:
        idTram,data,estatActual,estatPrevist
        1,20260801000057,1,0
    (Note: this differs from the live TRAMS_TRAMS.dat, which is hash-separated
    with no header. The monthly files are comma-separated WITH a header row.)
    """
    text = fetch_text(url)
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)  # skip 'idTram,data,estatActual,estatPrevist'
    for row in reader:
        if len(row) < 3:
            continue
        sid, ts, cur = row[0].strip(), row[1].strip(), row[2].strip()
        if not sid or not cur.isdigit():
            continue
        state = int(cur)
        if state == 0:
            continue  # 0 = no data / sensor down
        try:
            date = dt.date(int(ts[0:4]), int(ts[4:6]), int(ts[6:8]))
        except (ValueError, IndexError):
            continue
        rw = under_roadwork(intervals.get(sid, []), date)
        s = stats[sid]
        bucket = "rw" if rw else "norw"
        s[f"{bucket}_obs"] += 1
        if state >= CONGESTED_THRESHOLD:
            s[f"{bucket}_congested"] += 1
        if state >= DENSE_THRESHOLD:
            s[f"{bucket}_dense"] += 1


# ----------------------------------------------------------------------------
# 4. Main: compute per-section evidence and write summary
# ----------------------------------------------------------------------------
def main():
    print("Providence roadwork-evidence pipeline starting...", file=sys.stderr)

    geom = load_geometry()
    print(f"  geometry: {len(geom)} sections", file=sys.stderr)

    intervals = load_roadwork_intervals(geom)
    print(f"  roadwork intervals: {sum(len(v) for v in intervals.values())} "
          f"across {len(intervals)} sections", file=sys.stderr)

    resources = resolve_monthly_resources()
    window = resources[:WINDOW_MONTHS]
    print(f"  monthly files available: {len(resources)}, using newest {len(window)}",
          file=sys.stderr)

    stats = defaultdict(lambda: defaultdict(int))
    for (name, url) in window:
        print(f"  processing {name} ...", file=sys.stderr)
        try:
            accumulate_month(url, intervals, stats)
        except Exception as e:
            print(f"    skipped ({e})", file=sys.stderr)

    # build output rows
    sections = []
    for sid, s in stats.items():
        rw_obs, norw_obs = s["rw_obs"], s["norw_obs"]
        rw_cong = (s["rw_congested"] / rw_obs) if rw_obs else None
        norw_cong = (s["norw_congested"] / norw_obs) if norw_obs else None
        rw_dense = (s["rw_dense"] / rw_obs) if rw_obs else None
        norw_dense = (s["norw_dense"] / norw_obs) if norw_obs else None

        # "lift": how much more congested during roadworks vs not (dense-or-worse
        # is used because full congestion is rare and noisy)
        lift = None
        if rw_dense is not None and norw_dense and norw_dense > 0:
            lift = round(rw_dense / norw_dense, 2)

        enough = rw_obs >= MIN_OBSERVATIONS and norw_obs >= MIN_OBSERVATIONS
        sections.append({
            "id": sid,
            "rw_obs": rw_obs,
            "norw_obs": norw_obs,
            "rw_dense_rate": round(rw_dense, 3) if rw_dense is not None else None,
            "norw_dense_rate": round(norw_dense, 3) if norw_dense is not None else None,
            "lift": lift,
            "evidence": ("sufficient" if enough else "insufficient"),
        })

    sections.sort(key=lambda x: (x["lift"] or 0), reverse=True)

    out = {
        "generated": dt.datetime.utcnow().isoformat() + "Z",
        "window_months": WINDOW_MONTHS,
        "months_processed": len(window),
        "min_observations": MIN_OBSERVATIONS,
        "note": ("Per-section congestion frequency during active roadworks vs "
                 "not, over a rolling window. 'lift' = dense-or-worse rate with "
                 "roadwork / without. Lift > 1 means the section is congested "
                 "more often during roadworks. Co-location is evidence, not "
                 "proof of causation — busy streets attract both."),
        "sections": sections,
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"  wrote {OUTPUT_PATH}: {len(sections)} sections", file=sys.stderr)
    print("done.", file=sys.stderr)


if __name__ == "__main__":
    main()
