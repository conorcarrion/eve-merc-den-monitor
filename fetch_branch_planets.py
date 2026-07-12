"""
Fetch all temperate planets in one or more EVE Online regions.

Walks ESI (public endpoints, no auth) concurrently with aiohttp instead of
making thousands of sequential requests: region -> constellations -> systems
-> planets, then batch-resolves planet type names via /universe/names/
(up to 1000 ids per call) instead of one /universe/types/{id}/ call per
distinct type. Region names are resolved to ids with a single /universe/ids/
call instead of listing and checking every region.

A full CCP SDE download/parse was considered but is overkill for a one-off,
occasionally-rerun pull — it means shipping and versioning a multi-hundred-MB
archive and a YAML/sqlite schema just to save a few hundred HTTP requests
that aiohttp can now fire concurrently in seconds.

Usage:
    # config-driven: fetches every region listed in regions.json
    python fetch_branch_planets.py

    # ad hoc single region, ignores regions.json
    python fetch_branch_planets.py --region "Vale of the Silent"
    python fetch_branch_planets.py --region "Branch" --out somewhere/branch.csv

regions.json format:
    {
        "output_dir": "data",
        "planet_type": "Temperate",
        "regions": [
            "Branch",
            {"name": "Vale of the Silent", "planet_type": "Temperate"}
        ]
    }
Each entry in "regions" is either a plain region name (using the top-level
"planet_type") or an object with its own "planet_type" override.
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys

import aiohttp

BASE = "https://esi.evetech.net/latest"
HEADERS = {"User-Agent": "branch-den-scout (contact: your-eve-character-name)"}
CONCURRENCY = 20
MAX_RETRIES = 5
RETRYABLE_STATUSES = {420, 429, 500, 502, 503, 504}
DEFAULT_CONFIG_PATH = "regions.json"
DEFAULT_OUTPUT_DIR = "data"
DEFAULT_PLANET_TYPE = "Temperate"


async def fetch_json(session, method, url, semaphore, **kwargs):
    for attempt in range(MAX_RETRIES):
        async with semaphore:
            async with session.request(
                method, url, headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=20), **kwargs,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status in RETRYABLE_STATUSES:
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else 1.5 * (attempt + 1)
                else:
                    resp.raise_for_status()
        await asyncio.sleep(delay)
    raise RuntimeError(f"Gave up on {url} after {MAX_RETRIES} retries")


async def gather_json(session, method, urls, semaphore, **kwargs):
    return await asyncio.gather(*(fetch_json(session, method, u, semaphore, **kwargs) for u in urls))


async def resolve_region_id(session, semaphore, region_name):
    data = await fetch_json(session, "POST", f"{BASE}/universe/ids/", semaphore, json=[region_name])
    for r in data.get("regions") or []:
        if r["name"].lower() == region_name.lower():
            return r["id"]
    raise ValueError(f"Region '{region_name}' not found via ESI name resolution")


async def resolve_names(session, semaphore, ids):
    """Batch-resolve ids to names via /universe/names/ (max 1000 ids/call)."""
    ids = list(ids)
    names = {}
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        data = await fetch_json(session, "POST", f"{BASE}/universe/names/", semaphore, json=chunk)
        for item in data:
            names[item["id"]] = item["name"]
    return names


async def collect_rows(session, semaphore, region_name, planet_type_filter):
    region_id = await resolve_region_id(session, semaphore, region_name)
    region_data = await fetch_json(session, "GET", f"{BASE}/universe/regions/{region_id}/", semaphore)

    const_ids = region_data["constellations"]
    const_datas = await gather_json(
        session, "GET", [f"{BASE}/universe/constellations/{c}/" for c in const_ids], semaphore
    )

    system_ids = []
    system_constellation = {}
    for c_data in const_datas:
        for sid in c_data["systems"]:
            system_ids.append(sid)
            system_constellation[sid] = c_data["name"]

    system_datas = await gather_json(
        session, "GET", [f"{BASE}/universe/systems/{s}/" for s in system_ids], semaphore
    )

    planet_ids = []
    planet_system = {}
    for sys_data in system_datas:
        for p in sys_data.get("planets", []):
            planet_ids.append(p["planet_id"])
            planet_system[p["planet_id"]] = sys_data

    if not planet_ids:
        return []

    planet_datas = await gather_json(
        session, "GET", [f"{BASE}/universe/planets/{pid}/" for pid in planet_ids], semaphore
    )

    type_ids = {p["type_id"] for p in planet_datas}
    type_names = await resolve_names(session, semaphore, type_ids)

    rows = []
    for planet_data in planet_datas:
        type_name = type_names.get(planet_data["type_id"], "")
        if planet_type_filter.lower() not in type_name.lower():
            continue
        sys_data = planet_system[planet_data["planet_id"]]
        rows.append({
            "region": region_name,
            "constellation": system_constellation[sys_data["system_id"]],
            "system_name": sys_data["name"],
            "system_id": sys_data["system_id"],
            "security_status": round(sys_data.get("security_status", 0), 2),
            "planet_name": planet_data["name"],
            "planet_id": planet_data["planet_id"],
            "planet_type": type_name,
        })

    rows.sort(key=lambda r: (r["constellation"], r["system_name"], r["planet_name"]))
    return rows


async def fetch_regions(region_specs):
    """region_specs: list of (region_name, planet_type_filter) tuples.

    Returns {region_name: rows_or_exception} — one region's failure
    (typo'd name, transient ESI outage) doesn't abort the others.
    """
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(collect_rows(session, semaphore, name, planet_type) for name, planet_type in region_specs),
            return_exceptions=True,
        )
    return {name: result for (name, _), result in zip(region_specs, results)}


def slugify(region_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", region_name.lower()).strip("_")


def output_filename(region_name: str, planet_type: str) -> str:
    return f"{slugify(region_name)}_{planet_type.lower()}_planets.csv"


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    default_type = cfg.get("planet_type", DEFAULT_PLANET_TYPE)
    out_dir = cfg.get("output_dir", DEFAULT_OUTPUT_DIR)
    region_specs = []
    for entry in cfg.get("regions", []):
        if isinstance(entry, str):
            region_specs.append((entry, default_type))
        else:
            region_specs.append((entry["name"], entry.get("planet_type", default_type)))
    if not region_specs:
        raise ValueError(f"No regions listed in {path}")
    return region_specs, out_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", help="Fetch a single region ad hoc, ignoring --config")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Regions config (default: regions.json)")
    parser.add_argument("--out", help="Explicit output CSV path (single-region --region mode only)")
    parser.add_argument("--out-dir", help="Override the output directory")
    parser.add_argument("--planet-type", default=DEFAULT_PLANET_TYPE, help="Planet type filter for --region mode")
    args = parser.parse_args()

    if args.region:
        region_specs = [(args.region, args.planet_type)]
        out_dir = args.out_dir or DEFAULT_OUTPUT_DIR
    else:
        try:
            region_specs, cfg_out_dir = load_config(args.config)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"Error reading {args.config}: {e}", file=sys.stderr)
            sys.exit(1)
        out_dir = args.out_dir or cfg_out_dir

    try:
        results = asyncio.run(fetch_regions(region_specs))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    had_failure = False
    for region_name, planet_type in region_specs:
        result = results[region_name]

        if isinstance(result, Exception):
            print(f"  {region_name}: FAILED — {result}", file=sys.stderr)
            had_failure = True
            continue

        if not result:
            print(f"  {region_name}: no {planet_type} planets found — check region/type spelling.")
            continue

        out_path = args.out if (args.region and args.out) else os.path.join(out_dir, output_filename(region_name, planet_type))
        write_csv(out_path, result)
        print(f"  {region_name}: {len(result)} {planet_type} planets -> {out_path}")

    if had_failure:
        sys.exit(1)


if __name__ == "__main__":
    main()
