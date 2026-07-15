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
STATE_TTL_SECONDS = 1800  # time allowed to complete the SSO round trip — generous
# because it has to cover human login time (password, 2FA) plus a possible
# Streamlit Community Cloud cold-start/wake-up if the app was idle. This
# doesn't meaningfully widen any CSRF/replay window: EVE's authorization
# `code` is separately short-lived and single-use, so it's still the actual
# limiting factor, not this timestamp.

UNTAKEN_STATUS = "Untaken"
STATUS_OPTIONS = [UNTAKEN_STATUS, "Allied", "Hostile"]
UNKNOWN_STATUS = "Unknown"  # board placeholder for planets with no report yet
TIMER_INPUT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")
# 00d00h00m00s countdown, as shown in the client above a reinforced structure —
# every unit optional, but at least one must be present
RELATIVE_TIMER_RE = re.compile(
    r"(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?",
    re.IGNORECASE,
)

COLUMN_LABELS = {
    "system_name": "System",
    "planet_name": "Planet",
    "status": "Status",
    "owner": "Owner",
    "timer_end": "Reinforcement Timer",
    "time_left": "Time Left",
    "notes": "Notes",
    "updated_by": "Updated By",
}

ROMAN_NUMERAL_RE = re.compile(r"[IVXLCDM]+", re.IGNORECASE)
ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

TIME_LEFT_LOW_STYLE = "background-color: #f2c744; color: black"       # < 2h left, still counting down
TIME_LEFT_VULNERABLE_STYLE = "background-color: #e8720c; color: white"  # timer has passed
STATUS_STYLES = {
    "Allied": "background-color: #1f5fa8; color: white",
    "Hostile": "background-color: #a83232; color: white",
}


def roman_to_int(roman: str) -> int:
    total, prev = 0, 0
    for ch in reversed(roman.upper()):
        value = ROMAN_VALUES.get(ch, 0)
        total += -value if value < prev else value
        prev = max(prev, value)
    return total


def planet_designation(system_name: str, planet_name: str) -> str:
    """The bit of planet_name that isn't the system name, e.g. 'III'."""
    suffix = planet_name[len(system_name):].strip() if planet_name.startswith(system_name) else planet_name
    return suffix


def planet_number(system_name: str, planet_name: str) -> int:
    suffix = planet_designation(system_name, planet_name)
    return roman_to_int(suffix) if ROMAN_NUMERAL_RE.fullmatch(suffix) else 0


def format_planet_label(system_name: str, planet_name: str) -> str:
    """'BKG-Q2 III' -> '3 - III' — the system is already shown separately,
    and the number makes the dropdown sortable/scannable at a glance."""
    suffix = planet_designation(system_name, planet_name)
    if ROMAN_NUMERAL_RE.fullmatch(suffix):
        return f"{roman_to_int(suffix)} - {suffix.upper()}"
    return planet_name


def format_timer_end(timer_end):
    """Drop seconds/microseconds/UTC-offset noise; keep the date, since a
    reinforcement timer commonly runs past midnight and 'HH:MM' alone
    would be ambiguous about which day it lands on."""
    if pd.isnull(timer_end):
        return ""
    ts = pd.Timestamp(timer_end)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.strftime("%Y-%m-%d %H:%M")


def display_frame(df):
    df = df.copy()
    df["planet_name"] = [
        format_planet_label(s, p) for s, p in zip(df["system_name"], df["planet_name"])
    ]
    df["timer_end"] = df["timer_end"].apply(format_timer_end)
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
                owner TEXT,
                updated_by TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                resolved_at TIMESTAMPTZ
            )
        """))
        # idempotent for DBs created before these columns existed
        conn.execute(text("ALTER TABLE dens ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ"))
        conn.execute(text("ALTER TABLE dens ADD COLUMN IF NOT EXISTS owner TEXT"))
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


def verify_state(state):
    """None if valid, else a short reason — distinguishing 'expired' from
    'bad signature' is the whole point: they point at different bugs, and
    the old boolean-only version couldn't tell us which one was happening."""
    if not state or not isinstance(state, str) or "." not in state:
        return "malformed"
    ts_str, sig = state.split(".", 1)
    expected_sig = hmac.new(SECRET_KEY.encode(), ts_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return "bad_signature"
    try:
        ts = int(ts_str)
    except ValueError:
        return "malformed"
    if abs(time.time() - ts) > STATE_TTL_SECONDS:
        return "expired"
    return None


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
        state_error = verify_state(query_params.get("state"))
        if state_error:
            st.error(f"Login failed: state check failed ({state_error}). Please refresh the page and try again.")
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
        # target="_top" breaks out of any embedding frame (e.g. Streamlit
        # Cloud's "manage app" dashboard preview) so the SSO round trip
        # happens on the real top-level page, not trapped inside an iframe
        st.markdown(
            f'<a href="{build_auth_link(make_state())}" target="_top">'
            f'<strong>Log in with EVE Online SSO</strong></a>',
            unsafe_allow_html=True,
        )
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
                notes, owner, updated_by, updated_at
            FROM dens
            WHERE resolved_at IS NULL
            ORDER BY system_name, planet_name, updated_at DESC
        """), conn)


def current_report(reports_df, system_name, planet_name):
    """The existing row for this den, if any — used to carry forward
    fields an action isn't explicitly changing (e.g. owner), since every
    insert is a full snapshot and DISTINCT ON only looks at the latest row."""
    match = reports_df[
        (reports_df["system_name"] == system_name) & (reports_df["planet_name"] == planet_name)
    ]
    return None if match.empty else match.iloc[0]


def blank_if_na(value):
    return "" if pd.isna(value) else value


def parse_relative_timer(value: str, now: dt.datetime):
    """'1d2h30m' / '00d00h00m00s' -> now + that duration, or None."""
    match = RELATIVE_TIMER_RE.fullmatch(value)
    if not match or not any(match.groups()):
        return None
    parts = {k: int(v) for k, v in match.groupdict().items() if v is not None}
    return now + dt.timedelta(
        days=parts.get("days", 0), hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0), seconds=parts.get("seconds", 0),
    )


def parse_timer_end(value: str, now: dt.datetime):
    """Accepts either an absolute EVE/UTC timestamp ('2026-07-12 16:49:20')
    or a countdown ('00d00h00m00s', seconds optional) — whatever's shown
    above the structure when it reinforces, no head math required."""
    for fmt in TIMER_INPUT_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return parse_relative_timer(value, now)


def format_time_left(timer_end, now):
    if pd.isnull(timer_end):
        return ""
    end = pd.Timestamp(timer_end)
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    seconds_left = (end.to_pydatetime() - now).total_seconds()
    if seconds_left <= 0:
        return "Vulnerable"
    total_minutes = int(seconds_left // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}h{minutes:02d}m"


def highlight_status(row):
    styles = [""] * len(row)
    color = STATUS_STYLES.get(row["Status"])
    if color:
        styles[row.index.get_loc("Status")] = color
    return styles


def render_untaken_board(board):
    st.header("Untaken Dens")
    st.caption("Cleared out, but nobody's placed an Allied den here yet.")
    untaken = board[board["status"] == UNTAKEN_STATUS]
    if untaken.empty:
        st.caption("None right now.")
        return
    st.dataframe(display_frame(untaken), width="stretch")


def render_upcoming_timers(df, now):
    st.header("Upcoming timers")
    upcoming = df[df["reinforced"] & df["timer_end"].notna()].copy()
    upcoming = upcoming.sort_values("timer_end")

    if upcoming.empty:
        st.caption("No reinforced dens with an active timer.")
        return

    display = display_frame(upcoming)

    def highlight_time_left(row):
        styles = [""] * len(row)
        end = pd.Timestamp(row["Reinforcement Timer"])
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
        seconds_left = (end.to_pydatetime() - now).total_seconds()
        if seconds_left <= 0:
            style = TIME_LEFT_VULNERABLE_STYLE
        elif seconds_left < UPCOMING_TIMER_WARNING.total_seconds():
            style = TIME_LEFT_LOW_STYLE
        else:
            style = ""
        if style:
            styles[row.index.get_loc("Time Left")] = style
        return styles

    st.dataframe(
        display.style.apply(highlight_time_left, axis=1).apply(highlight_status, axis=1),
        width="stretch",
    )


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

    vulnerable = vulnerable.copy()
    vulnerable["_label"] = [
        f"{s} / {format_planet_label(s, p)}"
        for s, p in zip(vulnerable["system_name"], vulnerable["planet_name"])
    ]
    choice = st.selectbox("Den", vulnerable["_label"].tolist(), key="resolve_choice")
    outcome = st.selectbox("Outcome", STATUS_OPTIONS, key="resolve_outcome")

    if st.button("Save outcome"):
        chosen = vulnerable.loc[vulnerable["_label"] == choice].iloc[0]
        system_name, planet_name = chosen["system_name"], chosen["planet_name"]
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO dens (system_name, planet_name, status, reinforced,
                                           timer_end, notes, owner, updated_by)
                        VALUES (:system_name, :planet_name, :status, FALSE,
                                NULL, 'Reinforcement resolved', :owner, :updated_by)
                    """),
                    {
                        "system_name": system_name, "planet_name": planet_name,
                        "status": outcome, "owner": blank_if_na(chosen["owner"]),
                        "updated_by": st.session_state["character_name"],
                    },
                )
        except SQLAlchemyError as e:
            st.error(f"Could not save to the database: {e}")
            return
        st.success("Outcome saved.")
        st.rerun()


def render_owner_update(engine, reports_df):
    st.header("Update Den Owner")

    if reports_df.empty:
        st.caption("No reported dens yet.")
        return

    options = reports_df.copy()
    options["_label"] = [
        f"{s} / {format_planet_label(s, p)}"
        for s, p in zip(options["system_name"], options["planet_name"])
    ]
    choice = st.selectbox("Den", options["_label"].tolist(), key="owner_den_choice")
    chosen = options.loc[options["_label"] == choice].iloc[0]
    # key includes the den so switching dens shows *that* den's owner —
    # a static key would keep echoing back whatever was last typed, since
    # Streamlit ignores `value` for a key that already has session state
    owner_input = st.text_input("Owner", value=blank_if_na(chosen["owner"]), key=f"owner_input_{choice}")

    if st.button("Save owner"):
        timer_end = None if pd.isna(chosen["timer_end"]) else chosen["timer_end"].to_pydatetime()
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO dens (system_name, planet_name, status, reinforced,
                                           timer_end, notes, owner, updated_by)
                        VALUES (:system_name, :planet_name, :status, :reinforced,
                                :timer_end, :notes, :owner, :updated_by)
                    """),
                    {
                        "system_name": chosen["system_name"], "planet_name": chosen["planet_name"],
                        "status": chosen["status"], "reinforced": bool(chosen["reinforced"]),
                        "timer_end": timer_end, "notes": blank_if_na(chosen["notes"]),
                        "owner": owner_input.strip(),
                        "updated_by": st.session_state["character_name"],
                    },
                )
        except SQLAlchemyError as e:
            st.error(f"Could not save to the database: {e}")
            return
        st.success("Owner updated.")
        st.rerun()


def main():
    login_gate()
    engine = get_engine()
    now = dt.datetime.now(dt.timezone.utc)

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
            r["name"], key=f"region_{r['name']}", width="stretch",
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

    st.header("Report / update a den")
    col1, col2 = st.columns(2)
    with col1:
        system = st.selectbox("System", systems)
        planet_rows = planets_df[planets_df["system_name"] == system].copy()
        planet_rows["_number"] = [
            planet_number(system, p) for p in planet_rows["planet_name"]
        ]
        planet_rows["_label"] = [
            format_planet_label(system, p) for p in planet_rows["planet_name"]
        ]
        planet_rows = planet_rows.sort_values("_number")
        planet_label = st.selectbox("Planet", planet_rows["_label"].tolist())
        planet = planet_rows.loc[planet_rows["_label"] == planet_label, "planet_name"].iloc[0]
        status = st.selectbox("Status", STATUS_OPTIONS)
    with col2:
        reinforced = st.checkbox("Reinforced")
        timer_input = st.text_input(
            "Reinforcement ends (EVE/UTC time, optional)",
            placeholder="2026-07-12 16:49:20  or  00d00h00m",
        )
        notes = st.text_input("Notes (optional)")

    if st.button("Save den update", type="primary"):
        timer_end = None
        timer_input = timer_input.strip()
        if timer_input:
            timer_end = parse_timer_end(timer_input, now)
            if timer_end is None:
                st.error(
                    "Reinforcement time must be either an absolute EVE/UTC "
                    "timestamp like 2026-07-12 16:49:20, or a countdown like "
                    "00d00h00m (seconds optional)."
                )
                st.stop()
        # this form has no Owner field of its own — carry the existing
        # owner forward so filing a status update doesn't blank it out
        existing = current_report(reports_df, system, planet)
        owner = blank_if_na(existing["owner"]) if existing is not None else ""
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO dens (system_name, planet_name, status, reinforced,
                                           timer_end, notes, owner, updated_by)
                        VALUES (:system_name, :planet_name, :status, :reinforced,
                                :timer_end, :notes, :owner, :updated_by)
                    """),
                    {
                        "system_name": system, "planet_name": planet, "status": status,
                        "reinforced": reinforced, "timer_end": timer_end,
                        "notes": notes, "owner": owner,
                        "updated_by": st.session_state["character_name"],
                    },
                )
        except SQLAlchemyError as e:
            st.error(f"Could not save to the database: {e}")
            st.stop()
        st.success("Saved.")
        st.rerun()

    # every temperate planet in the region shows up on the board, even if
    # nobody has reported on it yet — that gap is the point, so oversight
    # holes are visible instead of just silently missing rows
    all_planets = planets_df[["system_name", "planet_name"]].drop_duplicates()
    board = all_planets.merge(reports_df, on=["system_name", "planet_name"], how="left")
    board["status"] = board["status"].fillna(UNKNOWN_STATUS)
    board["reinforced"] = board["reinforced"].fillna(False)
    board["notes"] = board["notes"].fillna("")
    board["owner"] = board["owner"].fillna("")
    board["updated_by"] = board["updated_by"].fillna("")

    board["time_left"] = board["timer_end"].apply(lambda t: format_time_left(t, now))
    board = board.sort_values(by="timer_end", na_position="last")

    render_upcoming_timers(board, now)
    render_resolve_outcome(engine, reports_df, now)

    st.header("Current board")
    st.dataframe(display_frame(board).style.apply(highlight_status, axis=1), width="stretch")

    render_untaken_board(board)
    render_owner_update(engine, reports_df)


if __name__ == "__main__":
    main()
