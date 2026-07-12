# Branch Den Tracker

Shared, alliance-gated board for tracking mercenary den status (active /
friendly / enemy, reinforced, timer) across temperate planets in Branch.

## 1. Get the planet list

```bash
pip install aiohttp
python fetch_branch_planets.py
```

This writes `branch_temperate_planets.csv`, which `app.py` reads for the
system/planet dropdowns. Re-run it if you want another region, or if CCP
adds/removes planets in Branch (rare, but wormhole-adjacent regions do
occasionally shift). It fetches concurrently over public ESI endpoints
(no auth needed) rather than parsing the full CCP SDE — fine for a
one-off/occasional pull; if you outgrow it, moving to the SDE would remove
the remaining per-planet ESI calls.

**Commit the resulting CSV to the repo.** Streamlit Community Cloud runs a
fresh clone of the repo on every deploy, so the app can only see the
planet list if it's checked in — running the fetch script locally and not
committing the CSV will break the "Report / update a den" dropdowns.

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

## Resolving a den

Dens are never deleted or overwritten in place — every report/resolve is a
new row (`dens` table is insert-only, for audit). "Current board" shows
only the latest report per system+planet where `resolved_at IS NULL`. Use
the "Resolve a den" section once a den is destroyed or flips sides to
insert a closing row and drop it off the board; the full history stays in
the table (query the DB directly if you need it — there's no history view
in the UI, by design, to keep this a single simple page).

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
- **OAuth `state` / CSRF check — flagged assumption:** the login flow now
  generates a random `state`, stores it in `st.session_state` before
  redirecting to EVE SSO, and verifies it matches on callback. This relies
  on Streamlit preserving `st.session_state` across the round trip to
  `login.eveonline.com` and back (the app already relied on this pre-existing
  behavior to remember `character_name` after login — the state check just
  adds a check on top of the same mechanism). In the normal case this holds
  because the browser reconnects to the same Streamlit session. If your
  deployment ever sits behind something that forces a new session on that
  redirect (e.g. certain proxy/load-balancer setups), the state check would
  fail closed and users would see a "please try again" error rather than a
  silent security hole — but it hasn't been tested against a real deployment,
  since that requires live SSO credentials. Test this once you have a
  registered app and a deployed URL.
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
