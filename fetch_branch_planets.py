"""
Fetch all temperate planets in a given EVE Online region.

Walks ESI (public endpoints, no auth) concurrently with aiohttp instead of
making thousands of sequential requests: region -> constellations -> systems
-> planets, then batch-resolves planet type names via /universe/names/
(up to 1000 ids per call) instead of one /universe/types/{id}/ call per
distinct type. The region name itself is resolved to an id with a single
/universe/ids/ call instead of listing and checking every region.

A full CCP SDE download/parse was considered but is overkill for a one-off,
occasionally-rerun pull — it means shipping and versioning a multi-hundred-MB
archive and a YAML/sqlite schema just to save a few hundred HTTP requests
that aiohttp can now fire concurrently in seconds.

Usage:
    python fetch_branch_planets.py
    python fetch_branch_planets.py --region "Branch" --out branch_temperate_planets.csv
"""

import argparse
import asyncio
import csv
import sys

import aiohttp

BASE = "https://esi.evetech.net/latest"
HEADERS = {"User-Agent": "branch-den-scout (contact: your-eve-character-name)"}
CONCURRENCY = 20
MAX_RETRIES = 5
RETRYABLE_STATUSES = {420, 429, 500, 502, 503, 504}


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


async def collect_rows(region_name, planet_type_filter):
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession() as session:
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="Branch")
    parser.add_argument("--out", default="branch_temperate_planets.csv")
    parser.add_argument("--planet-type", default="Temperate")
    args = parser.parse_args()

    try:
        rows = asyncio.run(collect_rows(args.region, args.planet_type))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print("No matching planets found — check region/type spelling.")
        return

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"Found {len(rows)} {args.planet_type} planets in {args.region}. Saved to {args.out}")


if __name__ == "__main__":
    main()
