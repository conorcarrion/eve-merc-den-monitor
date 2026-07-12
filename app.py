"""
Branch Den Tracker
------------------
Streamlit app, gated by EVE SSO + alliance check, backed by a shared
Postgres DB (Neon free tier works fine at this scale) so the whole
alliance sees the same live board.

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
import time
from base64 import b64encode
from urllib.parse import urlencode

import jwt
import pandas as pd
import requests
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

st.set_page_config(page_title="Branch Den Tracker", layout="wide")

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
    st.title("Branch Den Tracker")
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
        st.markdown(f"[**Log in with EVE Online SSO**]({build_auth_link(make_state())})")
        st.stop()


# ---------------------------------------------------------------------
# App
# ---------------------------------------------------------------------

@st.cache_data
def load_planet_list():
    # bundled CSV produced by fetch_branch_planets.py
    return pd.read_csv("branch_temperate_planets.csv")


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


def format_time_left(timer_end, now):
    if pd.isnull(timer_end):
        return ""
    end = pd.Timestamp(timer_end)
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    delta = end.to_pydatetime() - now
    if delta.total_seconds() <= 0:
        return "expired"
    return str(delta).split(".")[0]


def render_upcoming_timers(df, now):
    st.header("Upcoming timers")
    upcoming = df[df["reinforced"] & df["timer_end"].notna()].copy()
    upcoming = upcoming.sort_values("timer_end")

    if upcoming.empty:
        st.caption("No reinforced dens with an active timer.")
        return

    def highlight_urgent(row):
        end = pd.Timestamp(row["timer_end"])
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
        seconds_left = (end.to_pydatetime() - now).total_seconds()
        style = "background-color: #7a1f1f; color: white" if 0 < seconds_left < UPCOMING_TIMER_WARNING.total_seconds() else ""
        return [style] * len(row)

    display_cols = ["system_name", "planet_name", "status", "timer_end", "time_left", "notes", "updated_by"]
    st.dataframe(
        upcoming[display_cols].style.apply(highlight_urgent, axis=1),
        use_container_width=True,
    )


def render_resolve_form(engine, df):
    st.header("Resolve a den")
    if df.empty:
        st.caption("Nothing to resolve.")
        return

    options = [f"{r.system_name} / {r.planet_name}" for r in df.itertuples()]
    choice = st.selectbox("Den to mark resolved", options, key="resolve_choice")

    if st.button("Mark resolved"):
        system_name, planet_name = [s.strip() for s in choice.split("/", 1)]
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO dens (system_name, planet_name, status, reinforced,
                                           timer_end, notes, updated_by, resolved_at)
                        VALUES (:system_name, :planet_name, 'resolved', FALSE,
                                NULL, 'Marked resolved', :updated_by, now())
                    """),
                    {
                        "system_name": system_name, "planet_name": planet_name,
                        "updated_by": st.session_state["character_name"],
                    },
                )
        except SQLAlchemyError as e:
            st.error(f"Could not save to the database: {e}")
            return
        st.success("Marked resolved.")
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

    try:
        planets_df = load_planet_list()
    except FileNotFoundError:
        st.error(
            "branch_temperate_planets.csv not found. Run fetch_branch_planets.py "
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
        status = st.selectbox("Status", ["active", "friendly", "enemy"])
    with col2:
        reinforced = st.checkbox("Reinforced")
        hours = st.number_input("Timer - hours left", min_value=0, max_value=200, value=0)
        minutes = st.number_input("Timer - minutes left", min_value=0, max_value=59, value=0)
        notes = st.text_input("Notes (optional)")

    if st.button("Save den update", type="primary"):
        timer_end = None
        if hours or minutes:
            timer_end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours, minutes=minutes)
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
        df = load_board(engine)
    except SQLAlchemyError as e:
        st.error(f"Could not load the board from the database: {e}")
        st.stop()

    if df.empty:
        st.info("No dens reported yet.")
        return

    now = dt.datetime.now(dt.timezone.utc)
    df["time_left"] = df["timer_end"].apply(lambda t: format_time_left(t, now))
    df = df.sort_values(by="timer_end", na_position="last")

    render_upcoming_timers(df, now)

    st.header("Current board")
    st.dataframe(
        df[["system_name", "planet_name", "status", "reinforced",
            "timer_end", "time_left", "notes", "updated_by", "updated_at"]],
        use_container_width=True,
    )

    render_resolve_form(engine, df)


if __name__ == "__main__":
    main()
