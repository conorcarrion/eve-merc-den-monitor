# Mercenary Den Tracker

Shared, alliance-gated board for tracking mercenary den status (Untaken /
Allied / Hostile, reinforced, timer) across temperate planets. One board
per region listed in `regions.json`, switched with buttons in the sidebar.
Each board always lists every temperate planet in that region — ones with
no report yet show as `Unknown`, so gaps in coverage are visible rather
than just missing from the table.

## 1. Get the planet list

Regions to fetch are listed in `regions.json`:

```json
{
    "output_dir": "data",
    "planet_type": "Temperate",
    "regions": [
        "Branch"
    ]
}
```

```bash
pip install aiohttp
python fetch_branch_planets.py
```

With no `--region` flag, it reads `regions.json` and fetches every region
listed there, writing one CSV per region into `output_dir` (default
`data/`) — e.g. `data/branch_temperate_planets.csv`. `app.py` reads the
same `regions.json` (`load_regions()`) to build the sidebar's region
buttons and work out each region's CSV path, so the two stay in sync
automatically: add an entry to `regions.json`, run the fetch script,
commit the new CSV, and reboot the app (redeploy) — the region shows up
as a sidebar button with its own board, no code changes needed. Each
entry can be a plain region name or `{"name": "...", "planet_type": "..."}`
to override the type filter for just that region.

Reports aren't tagged with a region in the database — EVE system names are
globally unique, so each board is scoped by matching `system_name` against
that region's planet list. This means two regions can never accidentally
share a system, but it does mean planet names must stay consistent between
`regions.json`'s config and whatever CSV is actually sitting at that path.

For a one-off pull outside the config (e.g. trying a region before adding
it permanently), `--region` still works standalone and ignores
`regions.json`:
```bash
python fetch_branch_planets.py --region "Vale of the Silent"
```

Re-run the relevant region if CCP adds/removes planets there (rare, but
wormhole-adjacent regions do occasionally shift). Fetching is concurrent
over public ESI endpoints (no auth needed) rather than parsing the full
CCP SDE — fine for an occasional pull; if you outgrow it, moving to the
SDE would remove the remaining per-planet ESI calls.

**Commit the resulting CSVs in `data/` to the repo.** Streamlit Community
Cloud runs a fresh clone of the repo on every deploy, so the app can only
see the planet list if it's checked in — running the fetch script locally
and not committing the CSV will break the "Report / update a den"
dropdowns.

## 2. Get a free Postgres DB (Neon)

1. https://neon.tech -> new project -> copy the connection string.
2. That's your `DATABASE_URL`. The app creates its own `dens` table on
   first run — no manual schema setup needed.

## 3. Register an EVE SSO application

1. https://developers.eveonline.com/applications -> Create Application
2. Connection Type: **Authentication Only**
3. Scopes: none. This app only reads the identity JWT (`sub`/`name`
   claims) and the character's public `alliance_id` from the unauthenticated
   `/characters/{id}/` ESI endpoint — it never calls a scoped endpoint. The
   old `publicData` scope was retired by CCP some time ago; requesting it
   can make the SSO authorize step error out.
4. Callback URL: the exact URL your Streamlit app will live at, e.g.
   `https://branch-dens.streamlit.app` (you'll know this once you deploy,
   or reserve the name first in Streamlit Cloud before finishing this step)
5. Copy the Client ID and Secret Key.

## 4. Find your alliance ID

CCP retired the old public `/search/` endpoint. Resolve the name via
`/universe/ids/` instead (same endpoint `fetch_branch_planets.py` uses for
region names) — exact name match, case-insensitive:

```bash
curl -s "https://esi.evetech.net/latest/universe/ids/" \
  -X POST -H "Content-Type: application/json" \
  -d '["YOUR_ALLIANCE_NAME"]'
```

Returns `{"alliances": [{"id": ..., "name": "..."}]}` — that `id` is your
`ALLIANCE_ID`. (For Sigma Grindset, it's `99011223`.)

## 5. Deploy to Streamlit Community Cloud

1. Push this folder to a GitHub repo (private is fine, Streamlit Cloud
   supports private repos on your account).
2. https://share.streamlit.io -> New app -> point at the repo, main file
   `app.py`.
3. In app Settings > Secrets, paste in the values from
   `.streamlit/secrets.toml.example` filled in with your real values.
4. Deploy. Log in via the EVE SSO link on first load — only characters in
   the whitelisted alliance ID will get past the gate.

## Reporting a den

The `Planet` dropdown shows a number alongside the roman numeral (e.g.
`3 - III`) and is sorted by that number, instead of listing raw ESI planet
names in whatever order they happened to be in — easier to scan and pick
the right one at a glance. The System is already a separate field, so the
system name isn't repeated in the planet label; the same `N - ROMAN` format
is used everywhere else a planet is listed (board, resolve dropdown).

Reinforcement end time accepts two formats:
- an absolute EVE/UTC timestamp, e.g. `2026-07-12 16:49:20` (seconds optional)
- a countdown, e.g. `00d00h00m` or `1d2h30m` (seconds optional) — whatever's
  shown above the structure the moment it reinforces, so you can enter that
  directly instead of doing the date math yourself

Both are parsed as UTC — EVE time — regardless of the reporting character's
local timezone; a countdown is measured from the moment you hit save.
Leave the field blank if the den isn't reinforced or the timer isn't known
yet.

Status options are `Untaken`, `Allied`, `Hostile` — plus the board-only
`Unknown` placeholder for planets nobody has reported on. The first time
the app starts after this change, any existing rows using the old
`active`/`friendly`/`enemy` values are automatically renamed to
`Untaken`/`Allied`/`Hostile` (`init_db` in `app.py`) so the board doesn't
end up with a mix of old and new terminology. `Allied` rows are highlighted
blue and `Hostile` rows red on the board, wherever Status is shown.

## Resolving a reinforcement timer

Once a den's `Reinforcement Timer` passes, `Time Left` reads `Vulnerable`
instead of a countdown. In the "Upcoming timers" table, the `Time Left`
cell itself (not the whole row) is highlighted yellow once under 2 hours
remaining, and orange once it hits `Vulnerable`. The "Resolve Reinforcement
Outcome" section (above the current board) only lists dens that have hit
Vulnerable — pick the den and an outcome (`Allied` / `Hostile` / `Untaken`),
and it inserts a new report with that status and the timer cleared, so the
board reflects what actually happened once the den came out of
reinforcement.

Dens are never deleted or overwritten in place — every report/outcome is a
new row (`dens` table is insert-only, for audit). "Current board" shows
only the latest report per system+planet where `resolved_at IS NULL`
(that column still exists for a future hard-remove-from-board action, but
nothing in the UI sets it right now). The full history stays in the table
either way (query the DB directly if you need it — there's no history view
in the UI, by design, to keep this a single simple page).

Right below the current board, "Untaken Dens" is a filtered view showing
just the planets sitting at `Untaken` — cleared out (shot, reinforced,
resolved) but with nothing Allied placed there yet. It's the same board
data, just narrowed down to flag where there's an open opportunity to drop
a den.

## Updating a den's owner

`Owner` is a free-text field (e.g. a corp or character name) tracked
separately from `Status`. The "Update Den Owner" section at the bottom of
the page lists every currently reported den — reinforced or not — so you
can correct or set ownership without needing to file a full status report.
Because every save is a fresh full-row insert (see above), the main report
form and the outcome-resolve tool both carry the existing `Owner` value
forward untouched when they save, rather than each one having its own
Owner field and accidentally blanking it out.

## Rebooting / redeploying doesn't lose data

Den reports live in Neon Postgres, not in the Streamlit app process —
redeploying the app (new commit, manual reboot, Streamlit Cloud recycling
an idle container) only resets things held in memory: everyone's logged-in
session (they'll need to click through EVE SSO again) and any form field
someone was mid-typing. The `dens` table itself is untouched. Every schema
change this app makes on startup (`init_db`) is deliberately non-destructive
— `CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, and `UPDATE`s
that only rewrite text values — so redeploying to pick up a new region or a
code change is safe to do at any time. Adding a region to `regions.json`
specifically requires a reboot to show up (`load_regions()`/`load_planet_list()`
are cached for the process lifetime) — that's expected, not a sign
something's wrong.

## Notes / things to sanity-check

- The JWT verification in `exchange_code_for_character` follows EVE's
  current SSO v2 spec: token exchange at `v2/oauth/token`, RS256 signature
  verified against a key fetched via JWKS (JWKS location is discovered from
  the `.well-known/oauth-authorization-server` metadata endpoint rather
  than hardcoded, per CCP's recommendation), audience `EVE Online`, issuer
  `login.eveonline.com` or `https://login.eveonline.com` (both accepted —
  CCP has used either), and `sub` parsed as `CHARACTER:EVE:<id>`. CCP has
  changed SSO details before — if login fails with a claims/audience error,
  check https://developers.eveonline.com/docs/services/sso/ for the latest.
- **OAuth `state` / CSRF check:** originally implemented by stashing a
  random `state` in `st.session_state` before redirecting to EVE SSO and
  comparing on callback. That failed in practice on Streamlit Community
  Cloud — the full-page navigation to `login.eveonline.com` and back tears
  down and re-establishes the websocket, landing in a new session, so
  `st.session_state` set before the redirect was gone by the time the
  callback ran ("state parameter mismatch" on every login). Fixed by making
  `state` self-verifying instead of session-stored: it's an HMAC-signed
  timestamp (`make_state`/`verify_state` in `app.py`), checked against
  `EVE_SECRET_KEY` and a 10-minute freshness window on callback, with no
  server-side memory required between the redirect and the return.
- This checks alliance only, not corp — fine for "alliance members," but
  if you want corp-level gating instead, swap `alliance_id` for
  `corporation_id` in `check_alliance`.
- At <20 users hitting this occasionally, Streamlit Community Cloud's free
  tier and Neon's free tier are both comfortably within limits. Neon's free
  tier autosuspends its compute after a few minutes idle; `pool_pre_ping=True`
  on the engine handles the resulting stale-connection case, but the very
  first query after a long idle period may take a couple of extra seconds
  while Neon wakes back up — that's expected, not a bug.
- The SQLAlchemy engine is created once via `@st.cache_resource` rather than
  at module scope, since Streamlit re-runs the whole script on every
  interaction — without caching, every button click/rerun would open a new
  connection pool.
