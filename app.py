"""
Mercenary Den Tracker
----------------------
Streamlit app, gated by EVE SSO + alliance check, backed by a shared
Postgres DB (Neon free tier works fine at this scale) so the whole
alliance sees the same live board. One board per region listed in
regions.json, picked with the sidebar buttons.

Required secrets (Streamlit Cloud: Settings > Secrets), see
.streamlit/secrets.toml.example for the format:

    EVE_CLIENT_ID
    EVE_SECRET_KEY
    EVE_CALLBACK_URL      # must exactly match the app's public URL
    ALLIANCE_ID           # your alliance's numeric ID, as a whitelist
    DATABASE_URL          # postgres connection string

Register the app at https://developers.eveonline.com/applications with
Callback URL = EVE_CALLBACK_URL, Connection Type = Authentication Only,
and no scopes (this app only reads the identity JWT plus the character's
public alliance_id — it never calls a scoped ESI endpoint).
"""

import datetime as dt
import hashlib
import hmac
import json
import re
import time
from base64 import b64encode
from urllib.parse import urlencode

import jwt
import pandas as pd
import requests
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

st.set_page_config(page_title="Mercenary Den Tracker", layout="wide")

CLIENT_ID = st.secrets["EVE_CLIENT_ID"]
SECRET_KEY = st.secrets["EVE_SECRET_KEY"]
CALLBACK_URL = st.secrets["EVE_CALLBACK_URL"]
ALLOWED_ALLIANCE_ID = int(st.secrets["ALLIANCE_ID"])
DATABASE_URL = st.secrets["DATABASE_URL"]

AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize/"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
METADATA_URL = "https://login.eveonline.com/.well-known/oauth-authorization-server"
# EVE SSO accepts the JWT 'iss' claim in either form; CCP has flipped which
# one is issued before, so both are checked. See
# https://developers.eveonline.com/docs/services/sso/
ACCEPTED_ISSUERS = ["login.eveonline.com", "https://login.eveonline.com"]
UPCOMING_TIMER_WARNING = dt.timedelta(hours=2)
STATE_TTL_SECONDS = 600  # time allowed to complete the SSO round trip

STATUS_OPTIONS = ["Untaken", "Allied", "Hostile"]
UNKNOWN_STATUS = "Unknown"  # board placeholder for planets with no report yet
TIMER_INPUT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")

COLUMN_LABELS = {
    "system_name": "System",
    "planet_name": "Planet",
    "status": "Status",
    "timer_end": "Reinforcement Timer",
    "time_left": "Time Left",
    "notes": "Notes",
    "updated_by": "Updated By",
}


def display_frame(df):
    return df[list(COLUMN_LABELS.keys())].rename(columns=COLUMN_LABELS)


@st.cache_resource
def get_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=2)


def init_db(engine):
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS dens (
                id SERIAL PRIMARY KEY,
                system_name TEXT NOT NULL,
                planet_name TEXT NOT NULL,
                status TEXT NOT NULL,
                reinforced BOOLEAN NOT NULL DEFAULT FALSE,
                timer_end TIMESTAMPTZ,
                notes TEXT,
                updated_by TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                resolved_at TIMESTAMPTZ
            )
        """))
        # idempotent for DBs created before resolved_at existed
        conn.execute(text("ALTER TABLE dens ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ"))
        # one-time vocabulary rename (old status set -> Untaken/Allied/Hostile);
        # each UPDATE matches zero rows once the rename has happened, so this
        # is a cheap no-op on every startup after the first
        conn.execute(text("UPDATE dens SET status = 'Untaken' WHERE status = 'active'"))
        conn.execute(text("UPDATE dens SET status = 'Allied' WHERE status = 'friendly'"))
        conn.execute(text("UPDATE dens SET status = 'Hostile' WHERE status = 'enemy'"))


# ---------------------------------------------------------------------
# EVE SSO
# ---------------------------------------------------------------------

@st.cache_resource(ttl=3600)
def get_jwks_client():
    # CCP recommends discovering the JWKS location from the metadata
    # endpoint rather than hardcoding it, in case it ever moves.
    metadata = requests.get(METADATA_URL, timeout=10)
    metadata.raise_for_status()
    jwks_uri = metadata.json()["jwks_uri"]
    return jwt.PyJWKClient(jwks_uri)


def exchange_code_for_character(code: str):
    auth_header = b64encode(f"{CLIENT_ID}:{SECRET_KEY}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "authorization_code", "code": code},
        timeout=15,
    )
    resp.raise_for_status()
    access_token = resp.json()["access_token"]

    signing_key = get_jwks_client().get_signing_key_from_jwt(access_token)
    claims = jwt.decode(
        access_token,
        signing_key.key,
        algorithms=["RS256"],
        audience="EVE Online",
        issuer=ACCEPTED_ISSUERS,
    )
    # sub claim looks like "CHARACTER:EVE:2112625428"
    character_id = int(claims["sub"].split(":")[-1])
    character_name = claims["name"]
    return character_id, character_name


def check_alliance(character_id: int):
    resp = requests.get(
        f"https://esi.evetech.net/latest/characters/{character_id}/",
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("alliance_id")


def make_state() -> str:
    """
    HMAC-signed, self-verifying CSRF token for the OAuth 'state' param.

    Streamlit doesn't reliably keep st.session_state alive across the full
    browser round trip to login.eveonline.com and back — that navigation
    tears down and re-establishes the websocket, and can land in a brand
    new session, wiping anything stashed beforehand. So instead of
    "store a random value, compare on return," the state is signed with
    a timestamp: the callback can verify it originated from us (and is
    still fresh) without needing to remember anything server-side.
    Reuses EVE_SECRET_KEY as the HMAC key to avoid provisioning a second
    secret purely for this — it never leaves the server either way.
    """
    ts = str(int(time.time()))
    sig = hmac.new(SECRET_KEY.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def verify_state(state) -> bool:
    if not state or "." not in state:
        return False
    ts_str, sig = state.split(".", 1)
    expected_sig = hmac.new(SECRET_KEY.encode(), ts_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    return abs(time.time() - ts) <= STATE_TTL_SECONDS


def build_auth_link(state: str) -> str:
    params = {
        "response_type": "code",
        "redirect_uri": CALLBACK_URL,
        "client_id": CLIENT_ID,
        "state": state,
        # Authentication Only apps request no scopes — publicData was
        # retired years ago and this app never calls a scoped endpoint.
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def login_gate():
    query_params = st.query_params

    if "code" in query_params and "character_name" not in st.session_state:
        if not verify_state(query_params.get("state")):
            st.error(
                "Login failed: state parameter invalid or expired (possible CSRF "
                f"attempt, or the login took longer than {STATE_TTL_SECONDS // 60} minutes). "
                "Please try logging in again."
            )
            st.query_params.clear()
            st.stop()

        code = query_params["code"]
        try:
            char_id, char_name = exchange_code_for_character(code)
            alliance_id = check_alliance(char_id)
        except requests.RequestException as e:
            st.error(f"Login failed: could not reach EVE SSO/ESI ({e}). Please try again.")
            st.stop()
        except jwt.PyJWTError as e:
            st.error(f"Login failed: could not verify EVE SSO token ({e}).")
            st.stop()
        except (KeyError, ValueError) as e:
            st.error(f"Login failed: unexpected response from EVE SSO ({e}).")
            st.stop()

        if alliance_id != ALLOWED_ALLIANCE_ID:
            st.error("Your character isn't in the allowed alliance.")
            st.stop()

        st.session_state["character_name"] = char_name
        st.session_state["character_id"] = char_id
        st.query_params.clear()

    if "character_name" not in st.session_state:
        st.title("Mercenary Den Tracker")
        st.markdown(f"[**Log in with EVE Online SSO**]({build_auth_link(make_state())})")
        st.stop()


# ---------------------------------------------------------------------
# App
# ---------------------------------------------------------------------

REGIONS_CONFIG_PATH = "regions.json"
DEFAULT_PLANET_TYPE = "Temperate"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@st.cache_data
def load_regions():
    """Regions this app serves a board for, read from regions.json.

    Matches the format fetch_branch_planets.py reads/writes, so the same
    config drives both the data pull and which region tabs show up here.
    Cached for the life of the process — add a region, commit its CSV,
    and reboot the app (redeploy) to pick it up; no need for a live watch.
    """
    with open(REGIONS_CONFIG_PATH) as f:
        cfg = json.load(f)
    out_dir = cfg.get("output_dir", "data")
    default_type = cfg.get("planet_type", DEFAULT_PLANET_TYPE)
    regions = []
    for entry in cfg.get("regions", []):
        if isinstance(entry, str):
            name, planet_type = entry, default_type
        else:
            name, planet_type = entry["name"], entry.get("planet_type", default_type)
        csv_path = f"{out_dir}/{slugify(name)}_{planet_type.lower()}_planets.csv"
        regions.append({"name": name, "planet_type": planet_type, "csv_path": csv_path})
    return regions


@st.cache_data
def load_planet_list(csv_path):
    # bundled CSV produced by fetch_branch_planets.py (see regions.json)
    return pd.read_csv(csv_path)


def load_board(engine):
    with engine.begin() as conn:
        # latest non-resolved report per system+planet
        return pd.read_sql(text("""
            SELECT DISTINCT ON (system_name, planet_name)
                system_name, planet_name, status, reinforced, timer_end,
                notes, updated_by, updated_at
            FROM dens
            WHERE resolved_at IS NULL
            ORDER BY system_name, planet_name, updated_at DESC
        """), conn)


def parse_timer_end(value: str):
    """Parse an absolute EVE/UTC timestamp like '2026-07-12 16:49:20'."""
    for fmt in TIMER_INPUT_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def format_time_left(timer_end, now):
    if pd.isnull(timer_end):
        return ""
    end = pd.Timestamp(timer_end)
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    delta = end.to_pydatetime() - now
    if delta.total_seconds() <= 0:
        return "Vulnerable"
    return str(delta).split(".")[0]


def render_upcoming_timers(df, now):
    st.header("Upcoming timers")
    upcoming = df[df["reinforced"] & df["timer_end"].notna()].copy()
    upcoming = upcoming.sort_values("timer_end")

    if upcoming.empty:
        st.caption("No reinforced dens with an active timer.")
        return

    display = display_frame(upcoming)

    def highlight_urgent(row):
        end = pd.Timestamp(row["Reinforcement Timer"])
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
        seconds_left = (end.to_pydatetime() - now).total_seconds()
        style = "background-color: #7a1f1f; color: white" if 0 < seconds_left < UPCOMING_TIMER_WARNING.total_seconds() else ""
        return [style] * len(row)

    st.dataframe(display.style.apply(highlight_urgent, axis=1), use_container_width=True)


def render_resolve_outcome(engine, reports_df, now):
    st.header("Resolve Reinforcement Outcome")

    vulnerable = reports_df[
        reports_df["reinforced"]
        & reports_df["timer_end"].notna()
        & (reports_df["timer_end"] <= now)
    ]

    if vulnerable.empty:
        st.caption("No dens have hit Vulnerable yet.")
        return

    options = [f"{r.system_name} / {r.planet_name}" for r in vulnerable.itertuples()]
    choice = st.selectbox("Den", options, key="resolve_choice")
    outcome = st.selectbox("Outcome", STATUS_OPTIONS, key="resolve_outcome")

    if st.button("Save outcome"):
        system_name, planet_name = [s.strip() for s in choice.split("/", 1)]
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO dens (system_name, planet_name, status, reinforced,
                                           timer_end, notes, updated_by)
                        VALUES (:system_name, :planet_name, :status, FALSE,
                                NULL, 'Reinforcement resolved', :updated_by)
                    """),
                    {
                        "system_name": system_name, "planet_name": planet_name,
                        "status": outcome, "updated_by": st.session_state["character_name"],
                    },
                )
        except SQLAlchemyError as e:
            st.error(f"Could not save to the database: {e}")
            return
        st.success("Outcome saved.")
        st.rerun()


def main():
    login_gate()
    engine = get_engine()

    try:
        init_db(engine)
    except SQLAlchemyError as e:
        st.error(f"Could not reach the database: {e}")
        st.stop()

    st.sidebar.success(f"Logged in as {st.session_state['character_name']}")
    if st.sidebar.button("Log out"):
        for k in ("character_name", "character_id"):
            st.session_state.pop(k, None)
        st.rerun()

    regions = load_regions()
    if not regions:
        st.error(f"No regions listed in {REGIONS_CONFIG_PATH}.")
        st.stop()

    region_names = [r["name"] for r in regions]
    if st.session_state.get("selected_region") not in region_names:
        st.session_state["selected_region"] = region_names[0]

    st.sidebar.header("Regions")
    for r in regions:
        is_selected = r["name"] == st.session_state["selected_region"]
        if st.sidebar.button(
            r["name"], key=f"region_{r['name']}", use_container_width=True,
            type="primary" if is_selected else "secondary",
        ):
            st.session_state["selected_region"] = r["name"]
            st.rerun()

    region = next(r for r in regions if r["name"] == st.session_state["selected_region"])
    st.title(f"{region['name']} Den Tracker")

    try:
        planets_df = load_planet_list(region["csv_path"])
    except FileNotFoundError:
        st.error(
            f"{region['csv_path']} not found. Run fetch_branch_planets.py "
            "and commit the CSV to the repo before deploying."
        )
        st.stop()
    systems = sorted(planets_df["system_name"].unique())

    st.header("Report / update a den")
    col1, col2 = st.columns(2)
    with col1:
        system = st.selectbox("System", systems)
        planet_options = planets_df[planets_df["system_name"] == system]["planet_name"].tolist()
        planet = st.selectbox("Planet", planet_options)
        status = st.selectbox("Status", STATUS_OPTIONS)
    with col2:
        reinforced = st.checkbox("Reinforced")
        timer_input = st.text_input(
            "Reinforcement ends (EVE/UTC time, optional)",
            placeholder="2026-07-12 16:49:20",
        )
        notes = st.text_input("Notes (optional)")

    if st.button("Save den update", type="primary"):
        timer_end = None
        timer_input = timer_input.strip()
        if timer_input:
            timer_end = parse_timer_end(timer_input)
            if timer_end is None:
                st.error(
                    "Reinforcement time must look like 2026-07-12 16:49:20 "
                    "(EVE/UTC time, YYYY-MM-DD HH:MM:SS)."
                )
                st.stop()
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO dens (system_name, planet_name, status, reinforced,
                                           timer_end, notes, updated_by)
                        VALUES (:system_name, :planet_name, :status, :reinforced,
                                :timer_end, :notes, :updated_by)
                    """),
                    {
                        "system_name": system, "planet_name": planet, "status": status,
                        "reinforced": reinforced, "timer_end": timer_end,
                        "notes": notes, "updated_by": st.session_state["character_name"],
                    },
                )
        except SQLAlchemyError as e:
            st.error(f"Could not save to the database: {e}")
            st.stop()
        st.success("Saved.")
        st.rerun()

    try:
        reports_df = load_board(engine)
    except SQLAlchemyError as e:
        st.error(f"Could not load the board from the database: {e}")
        st.stop()

    # the dens table has no region column — EVE system names are globally
    # unique, so scoping to this region's systems is enough to keep other
    # regions' reports (once there are any) off this board
    region_systems = set(planets_df["system_name"])
    reports_df = reports_df[reports_df["system_name"].isin(region_systems)]

    # every temperate planet in the region shows up on the board, even if
    # nobody has reported on it yet — that gap is the point, so oversight
    # holes are visible instead of just silently missing rows
    all_planets = planets_df[["system_name", "planet_name"]].drop_duplicates()
    board = all_planets.merge(reports_df, on=["system_name", "planet_name"], how="left")
    board["status"] = board["status"].fillna(UNKNOWN_STATUS)
    board["reinforced"] = board["reinforced"].fillna(False)
    board["notes"] = board["notes"].fillna("")
    board["updated_by"] = board["updated_by"].fillna("")

    now = dt.datetime.now(dt.timezone.utc)
    board["time_left"] = board["timer_end"].apply(lambda t: format_time_left(t, now))
    board = board.sort_values(by="timer_end", na_position="last")

    render_upcoming_timers(board, now)
    render_resolve_outcome(engine, reports_df, now)

    st.header("Current board")
    st.dataframe(display_frame(board), use_container_width=True)


if __name__ == "__main__":
    main()
