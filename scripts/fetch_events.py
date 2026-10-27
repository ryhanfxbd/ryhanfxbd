#!/usr/bin/env python3
"""BDlivetv Events Bot.

Builds app/events.json — the fixtures feed the app's Events tab
reads — from free public sports-data sources:

  1. TheSportsDB (free test key): per-day fixtures for Cricket,
     Soccer, Basketball, Tennis and Fighting, plus the livescore
     feed for Cricket/Soccer/Basketball (the free key caps the day
     endpoint at a few events, so the livescore feed backfills
     today's matches that already started).
  2. ESPN's public scoreboard JSON (no key): per-day fixtures for
     the major football leagues (Premier League, La Liga, Serie A,
     Bundesliga, Ligue 1, Champions League), the NBA, and the IPL —
     the reliable backbone for the leagues viewers actually watch.

Window: yesterday 00:00 UTC through +3 days. The app derives
Live/Today from startUtc itself (Cricket stays "live" 8h, Football
3h, anything else 4h), so this file stores fixtures only — no
status field to maintain.

Channel links are collected from the playlists themselves
(app/*.m3u) in three passes: dedicated event channels whose own
name carries the fixture (both teams, team + competition, or a
dedicated league brand), then a league broadcaster table whose
candidate names are each verified against the corpus at runtime.
ALL verified matches are collected — up to five per event — into
the event's "channels" list (dedicated channels first, then
broadcasters); "channel" keeps the first one for older app
builds. There is deliberately NO generic sport-level fallback —
a wrong Watch channel is worse than none, so an event with no
verified channel stays info-only in the app.

One fallback DOES exist (owner's rule, 2026-10-24): when an
event's primary channel is not carried by the app's own
playlists at all, the free public corpus (data/*.m3u — the
iptv-org sports index) may lend its stream URLs for that channel
name: the probed-alive ones are written as the event's
"freeUrls" and the app plays them as a synthetic free channel.
The app's own playlists always win first; free URLs only ever
fill a gap, never replace a carried channel.

If every source fails, nothing is written and the script exits
non-zero — a good events.json is never replaced by an empty one.

Stdlib only. Runs at the end of the Auto Update refresh
(scripts/refresh_playlists.py) and on its own:

    python3 scripts/fetch_events.py
"""

import gzip
import json
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

socket.setdefaulttimeout(30)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from categorize_channels import clean_name, norm_key  # noqa: E402

GENERATOR = "BDlivetv Events Bot"
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "app" / "events.json"
APP_DIR = REPO_ROOT / "app"

TSDB_BASE = "https://www.thesportsdb.com/api/v1/json/123"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

UA = "BDlivetvEventsBot/1.0 (https://github.com/ryhanfxbd/ryhanfxbd)"
# ESPN's CDN refuses bare bot agents; this still names the bot, in
# the standard "compatible" form polite crawlers use.
UA_ESPN = ("Mozilla/5.0 (compatible; BDlivetvEventsBot/1.0; "
           "+https://github.com/ryhanfxbd/ryhanfxbd)")
TIMEOUT = 15
MAX_EVENTS = 200

# Owner's rule (2026-10-23): the Events feed carries CRICKET and
# FOOTBALL only — no other sports are fetched or written.
# TSDB sport name -> app sport name ("Soccer" is shown as Football).
TSDB_DAY_SPORTS = ("Cricket", "Soccer")
TSDB_LIVE_SPORTS = ("Cricket", "Soccer")
SPORT_MAP = {"Soccer": "Football"}
ALLOWED_SPORTS = ("Cricket", "Football")

# ESPN league paths -> app sport. Football majors + IPL.
ESPN_LEAGUES = (
    ("soccer/eng.1", "Football"),
    ("soccer/esp.1", "Football"),
    ("soccer/ita.1", "Football"),
    ("soccer/ger.1", "Football"),
    ("soccer/fra.1", "Football"),
    ("soccer/uefa.champions", "Football"),
    ("cricket/8048", "Cricket"),
)

# League-name fragments that mark an event as "major" when the
# 200-event cap forces a trim.
MAJOR_LEAGUE_HINTS = (
    "english premier league", "premier league", "la liga",
    "serie a", "bundesliga", "ligue 1", "champions league",
    "indian premier league", "ipl", "nba", "icc", "asia cup",
    "world cup", "bangladesh premier league",
)

# ---------------------------------------------------------------------------
# Country flags — national teams get a flag emoji; club teams fall
# back to the source's badge/logo URL.
# ---------------------------------------------------------------------------

def _flag(code):
    """Regional-indicator flag emoji for an ISO 3166 alpha-2 code."""
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code)


_FLAG_BY_NAME = {}


def _add_flags(code, *names):
    for nm in names:
        _FLAG_BY_NAME[nm] = _flag(code)


_add_flags("BD", "bangladesh")
_add_flags("IN", "india")
_add_flags("PK", "pakistan")
_add_flags("LK", "sri lanka")
_add_flags("AF", "afghanistan")
_add_flags("NP", "nepal")
_add_flags("AU", "australia")
_add_flags("NZ", "new zealand")
_add_flags("ZA", "south africa")
_add_flags("ZW", "zimbabwe")
_add_flags("IE", "ireland")
_add_flags("NL", "netherlands")
_add_flags("AE", "uae", "united arab emirates")
_add_flags("US", "usa", "united states", "united states of america")
_add_flags("AR", "argentina")
_add_flags("BR", "brazil")
_add_flags("FR", "france")
_add_flags("DE", "germany")
_add_flags("ES", "spain")
_add_flags("IT", "italy")
_add_flags("PT", "portugal")
_add_flags("BE", "belgium")
_add_flags("HR", "croatia")
_add_flags("UY", "uruguay")
_add_flags("JP", "japan")
_add_flags("KR", "south korea", "korea republic", "republic of korea")
_add_flags("IR", "iran")
_add_flags("SA", "saudi arabia")
_add_flags("QA", "qatar")
_add_flags("MA", "morocco")
_add_flags("SN", "senegal")
_add_flags("GH", "ghana")
_add_flags("NG", "nigeria")
_add_flags("CM", "cameroon")
_add_flags("MX", "mexico")
_add_flags("CA", "canada")
_add_flags("DK", "denmark")
_add_flags("CH", "switzerland")
_add_flags("AT", "austria")
_add_flags("PL", "poland")
_add_flags("TR", "turkey", "türkiye", "turkiye")
_add_flags("UA", "ukraine")
_add_flags("SE", "sweden")
_add_flags("NO", "norway")
_add_flags("EG", "egypt")
_add_flags("DZ", "algeria")
_add_flags("TN", "tunisia")
_add_flags("ID", "indonesia")
_add_flags("MY", "malaysia")
_add_flags("TH", "thailand")
_add_flags("VN", "vietnam")
_add_flags("CN", "china", "china pr")
_add_flags("IQ", "iraq")
_add_flags("JO", "jordan")
_add_flags("UZ", "uzbekistan")
_add_flags("OM", "oman")
_add_flags("BH", "bahrain")
_add_flags("KW", "kuwait")
_add_flags("MV", "maldives")
_add_flags("BT", "bhutan")
_add_flags("HK", "hong kong")
_add_flags("SG", "singapore")
_add_flags("PH", "philippines")
_add_flags("MM", "myanmar")
_add_flags("KH", "cambodia")
_add_flags("LA", "laos")
_add_flags("PS", "palestine")
_add_flags("LB", "lebanon")
_add_flags("SY", "syria")
_add_flags("YE", "yemen")
_add_flags("KE", "kenya")
_add_flags("NA", "namibia")
_add_flags("UG", "uganda")
_add_flags("TZ", "tanzania")
_add_flags("PG", "papua new guinea")
_add_flags("FJ", "fiji")
_add_flags("GR", "greece")
_add_flags("CZ", "czechia", "czech republic")
_add_flags("RO", "romania")
_add_flags("HU", "hungary")
_add_flags("RS", "serbia")
_add_flags("CL", "chile")
_add_flags("CO", "colombia")
_add_flags("PE", "peru")
_add_flags("EC", "ecuador")
_add_flags("PY", "paraguay")
_add_flags("VE", "venezuela")
_add_flags("NG", "nigeria")

# Subdivision flags (England / Scotland / Wales) are tag sequences,
# not regional indicators.
_FLAG_BY_NAME["england"] = "\U0001F3F4\U000E0067\U000E0062\U000E0065" \
                          "\U000E006E\U000E0067\U000E007F"
_FLAG_BY_NAME["scotland"] = "\U0001F3F4\U000E0067\U000E0062\U000E0073" \
                            "\U000E0063\U000E0074\U000E007F"
_FLAG_BY_NAME["wales"] = "\U0001F3F4\U000E0067\U000E0062\U000E0077" \
                         "\U000E006C\U000E0073\U000E007F"


def flag_for(team_name, badge_url):
    """National team -> flag emoji; anything else -> the source's
    badge/logo URL when there is one, else empty."""
    emoji = _FLAG_BY_NAME.get(norm_key(team_name))
    if emoji:
        return emoji
    return (badge_url or "").strip()


# ---------------------------------------------------------------------------
# HTTP — stdlib JSON GET with one retry on transient failures.
# ---------------------------------------------------------------------------

def _get_json(url, ua):
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": ua,
                              "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                print("  fetch failed: %s (%s)" % (url, e), flush=True)
                return None
            time.sleep(1.0)
    return None


# ---------------------------------------------------------------------------
# Time + id helpers
# ---------------------------------------------------------------------------

def _parse_tsdb_time(ev):
    """TSDB timestamps are UTC. Prefers strTimestamp; falls back to
    dateEvent + strTime. Returns an aware UTC datetime or None."""
    ts = (ev.get("strTimestamp") or "").strip()
    for candidate, fmt in ((ts, "%Y-%m-%dT%H:%M:%S"),
                           (ts, "%Y-%m-%dT%H:%M"),
                           ((ev.get("dateEvent") or "") + " " +
                            (ev.get("strTime") or ""),
                            "%Y-%m-%d %H:%M:%S"),
                           ((ev.get("dateEvent") or "") + " " +
                            (ev.get("strTime") or ""), "%Y-%m-%d %H:%M")):
        if not candidate.strip():
            continue
        try:
            return datetime.strptime(candidate.strip(), fmt).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_espn_time(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(
            value, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _slug(text):
    folded = unicodedata.normalize("NFKD", text or "")
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-",
                  ascii_only.lower()).strip("-")


def _team_key(name):
    """Normalization for cross-source dedupe: case/space-insensitive,
    trailing sport suffixes and common abbreviations unified."""
    key = norm_key(name)
    key = re.sub(r"\s+(cricket|football club|fc|sc)$", "", key)
    key = key.replace(" utd", " united")
    return key


# ---------------------------------------------------------------------------
# Source 1 — TheSportsDB
# ---------------------------------------------------------------------------

def _tsdb_event(ev, sport):
    home = (ev.get("strHomeTeam") or "").strip()
    away = (ev.get("strAwayTeam") or "").strip()
    if sport == "Cricket":
        # TSDB suffixes national sides: "India Cricket".
        home = re.sub(r"\s+Cricket$", "", home)
        away = re.sub(r"\s+Cricket$", "", away)
    start = _parse_tsdb_time(ev)
    if not home or not away or home == away or start is None:
        return None
    return {
        "sport": SPORT_MAP.get(sport, sport),
        "league": (ev.get("strLeague") or "").strip(),
        "teamA": home,
        "teamB": away,
        "start": start,
        "badgeA": (ev.get("strHomeTeamBadge") or "").strip(),
        "badgeB": (ev.get("strAwayTeamBadge") or "").strip(),
        "source": "tsdb",
    }


def fetch_tsdb(days):
    out = []
    ok = 0
    for sport in TSDB_DAY_SPORTS:
        for day in days:
            url = "%s/eventsday.php?d=%s&s=%s" % (
                TSDB_BASE, day.strftime("%Y-%m-%d"),
                urllib.parse.quote(sport))
            data = _get_json(url, UA)
            if data is None:
                continue
            ok += 1
            for ev in data.get("events") or []:
                parsed = _tsdb_event(ev, sport)
                if parsed:
                    out.append(parsed)
    for sport in TSDB_LIVE_SPORTS:
        data = _get_json("%s/livescore.php?s=%s"
                         % (TSDB_BASE, urllib.parse.quote(sport)), UA)
        if data is None:
            continue
        ok += 1
        rows = data.get("livescore") or data.get("events") or []
        for ev in rows:
            parsed = _tsdb_event(ev, sport)
            if parsed:
                out.append(parsed)
    print("  TheSportsDB: %d usable response(s), %d raw event(s)"
          % (ok, len(out)), flush=True)
    return out, ok


# ---------------------------------------------------------------------------
# Source 2 — ESPN public scoreboards
# ---------------------------------------------------------------------------

def _espn_event(ev, league_name, sport):
    start = _parse_espn_time(ev.get("date"))
    comps = ev.get("competitions") or []
    if start is None or not comps:
        return None
    home = away = None
    home_logo = away_logo = ""
    for comp in comps[0].get("competitors") or []:
        team = comp.get("team") or {}
        name = (team.get("displayName")
                or team.get("name") or "").strip()
        logo = (team.get("logo") or "").strip()
        if comp.get("homeAway") == "home":
            home, home_logo = name, logo
        elif comp.get("homeAway") == "away":
            away, away_logo = name, logo
    if not home or not away:
        return None
    return {
        "sport": sport,
        "league": league_name,
        "teamA": home,
        "teamB": away,
        "start": start,
        "badgeA": home_logo,
        "badgeB": away_logo,
        "source": "espn",
    }


def fetch_espn(days):
    out = []
    ok = 0
    for path, sport in ESPN_LEAGUES:
        for day in days:
            url = "%s/%s/scoreboard?dates=%s" % (
                ESPN_BASE, path, day.strftime("%Y%m%d"))
            data = _get_json(url, UA_ESPN)
            if data is None:
                continue
            ok += 1
            leagues = data.get("leagues") or []
            league_name = ((leagues[0].get("name") if leagues else "")
                           or "").strip()
            for ev in data.get("events") or []:
                parsed = _espn_event(ev, league_name, sport)
                if parsed:
                    out.append(parsed)
            time.sleep(0.1)
    print("  ESPN: %d usable response(s), %d raw event(s)"
          % (ok, len(out)), flush=True)
    return out, ok


# ---------------------------------------------------------------------------
# Channel mapping — collected from the playlists themselves.
#
# A channel may only be attached when it EXISTS in app/*.m3u (the
# app matches by normalized name; HD/SD are part of a channel's
# identity, trailing 720p-style tokens are ignored upstream by
# clean_name). Three passes, in priority order:
#
#   A — dedicated event channels: the channel's own name carries
#       the fixture — BOTH teams' tokens, one team's token plus
#       the competition's token, or a dedicated league brand
#       (e.g. "LIVE EVENTS IPL (CTV)", "DAZN LaLiga").
#   B — league broadcaster table: candidate names per league /
#       team, each verified against the corpus at runtime and
#       skipped silently when absent; candidates are in flagship
#       order (HD form, lowest number). Every matching rule
#       contributes all of its verified candidates — a fixture
#       carried by two broadcasters lists both.
#   C — no channel (info-only card). Deliberately NO generic
#       sport-level fallback: a wrong Watch channel is worse
#       than none.
#
# Passes A and B are concatenated (A first), deduplicated and
# capped at MAX_CHANNELS: one fixture often airs on several of
# the owner's channels, and the app offers each of them.
# ---------------------------------------------------------------------------

def load_channel_names(app_dir):
    """norm_key(cleaned name) -> display name, from every app/*.m3u
    plus the corpus-only lists under data/ (e.g. the public iptv-org
    sports index — kept OUT of app/ on the owner's instruction so its
    channels never become an app source or appear in Home/Live TV,
    while event mapping may still reference its channel names)."""
    by_key = {}
    paths = list(app_dir.glob("*.m3u"))
    data_dir = app_dir.parent / "data"
    if data_dir.is_dir():
        paths += list(data_dir.glob("*.m3u"))
    for path in sorted(paths, key=lambda p: p.name.lower()):
        text = path.read_text(encoding="utf-8", errors="replace")
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#EXTINF"):
                name = line.rsplit(",", 1)[-1].strip() \
                    if "," in line else ""
                cleaned = clean_name(name)
                if cleaned:
                    by_key.setdefault(norm_key(cleaned), cleaned)
    return by_key


def load_app_channel_keys(app_dir):
    """norm_keys of channels carried by the APP's own playlists
    (app/*.m3u only — NOT the corpus-only data/ lists). Mapping
    prefers these for the primary slot: a corpus-only name (e.g.
    an iptv-org index channel the app never loads) renders greyed
    in the app and its card tap dead-ends, so it must not headline
    an event when an app-carried channel also covers it."""
    keys = set()
    for path in sorted(app_dir.glob("*.m3u"),
                       key=lambda p: p.name.lower()):
        text = path.read_text(encoding="utf-8", errors="replace")
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#EXTINF"):
                name = line.rsplit(",", 1)[-1].strip() \
                    if "," in line else ""
                cleaned = clean_name(name)
                if cleaned:
                    keys.add(norm_key(cleaned))
    return keys


def load_channel_health(app_dir):
    """norm_key(cleaned name) -> True when the channel has at least
    one stream the Auto Update robot's latest probe did NOT prove
    dead, False when EVERY stream it carries probed dead.

    Reads app/playlist_status.json (written by refresh_playlists.py
    earlier in the same run) for the dead-URL set, then walks the
    same corpus files as load_channel_names collecting each
    channel's URLs. A URL counts as dead only when the probe put it
    on a dead list — 'unknown'/'restricted'/unprobed URLs keep the
    channel healthy, because the runner's view is not the viewer's:
    this only demotes channels the probe positively killed on every
    server. Any failure (no status file, malformed JSON) yields {}
    — every channel counts as healthy and mapping is unchanged."""
    try:
        status = json.loads((app_dir / "playlist_status.json")
                            .read_text(encoding="utf-8"))
        dead_urls = set()
        for info in (status.get("sources") or {}).values():
            if not isinstance(info, dict):
                continue
            dead = info.get("dead")
            if isinstance(dead, list):
                for entry in dead:
                    if isinstance(entry, dict) and entry.get("url"):
                        dead_urls.add(entry["url"].strip())
        if not dead_urls:
            return {}
        urls_by_key = {}
        paths = list(app_dir.glob("*.m3u"))
        data_dir = app_dir.parent / "data"
        if data_dir.is_dir():
            paths += list(data_dir.glob("*.m3u"))
        for path in sorted(paths, key=lambda p: p.name.lower()):
            text = path.read_text(encoding="utf-8", errors="replace")
            pending = None
            for raw in text.splitlines():
                line = raw.strip()
                if line.startswith("#EXTINF"):
                    name = line.rsplit(",", 1)[-1].strip() \
                        if "," in line else ""
                    cleaned = clean_name(name)
                    pending = norm_key(cleaned) if cleaned else None
                elif line and not line.startswith("#"):
                    if pending:
                        urls_by_key.setdefault(pending, []).append(line)
                    pending = None
        return {key: any(u not in dead_urls for u in urls)
                for key, urls in urls_by_key.items() if urls}
    except Exception:  # noqa: BLE001 — health is best-effort
        return {}


def load_free_pool(app_dir):
    """The free public corpus (data/*.m3u — e.g. the iptv-org
    sports index): norm_key(cleaned name) -> {"name": display,
    "urls": [stream URLs in file order, deduped]}. Kept apart
    from the app sources on the owner's instruction — these
    streams never become an app playlist; the events feed only
    borrows their URLs as a fallback for events whose channel
    the app does not carry (see attach_free_urls)."""
    pool = {}
    data_dir = app_dir.parent / "data"
    if not data_dir.is_dir():
        return pool
    for path in sorted(data_dir.glob("*.m3u"),
                       key=lambda p: p.name.lower()):
        text = path.read_text(encoding="utf-8", errors="replace")
        pending = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#EXTINF"):
                name = line.rsplit(",", 1)[-1].strip() \
                    if "," in line else ""
                cleaned = clean_name(name)
                pending = norm_key(cleaned) if cleaned else None
                if pending and pending not in pool:
                    pool[pending] = {"name": cleaned, "urls": []}
            elif line and not line.startswith("#"):
                if pending:
                    urls = pool[pending]["urls"]
                    if line not in urls:
                        urls.append(line)
                pending = None
    return pool


def probe_url_ok(url):
    """Light liveness probe for a free-pool stream URL: True only
    when the GET answers 2xx (redirects followed) with a body
    that does not start as an HTML page — an HTML landing page is
    a dead end, not a stream, the same honesty rule the playlist
    probe uses. Anything else (error, timeout, non-2xx) is
    not-ok."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA_ESPN})
        with urllib.request.urlopen(req, timeout=8) as resp:
            if not (200 <= resp.status < 300):
                return False
            head = resp.read(512).lstrip().lower()
            return not (head.startswith(b"<!doctype")
                        or head.startswith(b"<html"))
    except Exception:  # noqa: BLE001
        return False


def attach_free_urls(events, app_keys, pool):
    """Owner's Events fallback rule: an event whose PRIMARY
    channel is not carried by the app's own playlists at all may
    still be playable from the free public corpus. For every
    such event, probe the pool URLs filed under the primary's
    name and keep the ones that answer as real streams (pool
    file order preserved), capped at 4, as the event's
    "freeUrls". Events whose primary the app carries never get
    freeUrls — the owner's playlists always come first — and a
    name absent from the pool (or with no answering URL) keeps
    the event exactly as mapped. Returns how many events got
    freeUrls. Best-effort: a probing failure attaches nothing."""
    wanted = {}
    for ev in events:
        primary = ev.get("channel") or ""
        if not primary:
            continue
        key = norm_key(primary)
        if key in app_keys or key not in pool:
            continue
        wanted.setdefault(key, pool[key]["urls"])
    if not wanted:
        return 0
    ok_by_url = {}
    all_urls = sorted({u for urls in wanted.values() for u in urls})
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for url, ok in ex.map(
                    lambda u: (u, probe_url_ok(u)), all_urls):
                ok_by_url[url] = ok
    except Exception:  # noqa: BLE001 — probing is best-effort
        return 0
    attached = 0
    for ev in events:
        primary = ev.get("channel") or ""
        if not primary:
            continue
        urls = wanted.get(norm_key(primary))
        if not urls:
            continue
        ok_urls = [u for u in urls if ok_by_url.get(u)][:4]
        if ok_urls:
            ev["freeUrls"] = ok_urls
            attached += 1
    return attached


def _tokens(text):
    """Word tokens of a name (emoji / punctuation dropped)."""
    return set(re.findall(r"[a-z0-9]+", norm_key(text)))


# National-team match tokens: the country name's words plus the
# codes broadcasters actually put in channel names. Any ONE token
# present in a channel name counts as that team's mark.
NATIONAL_TOKENS = {}


def _nat(name, *tokens):
    NATIONAL_TOKENS[norm_key(name)] = set(tokens)


_nat("bangladesh", "bangladesh", "ban", "bd")
_nat("india", "india", "ind")
_nat("pakistan", "pakistan", "pak")
_nat("sri lanka", "sri", "lanka", "sl", "lka")
_nat("afghanistan", "afghanistan", "afg")
_nat("new zealand", "new", "zealand", "nz", "nzl")
_nat("australia", "australia", "aus")
_nat("england", "england", "eng")
_nat("south africa", "south", "africa", "sa", "rsa")
_nat("west indies", "west", "indies", "wi")
_nat("zimbabwe", "zimbabwe", "zim")
_nat("ireland", "ireland", "ire")
_nat("nepal", "nepal", "nep")
_nat("uae", "uae", "emirates")
_nat("united arab emirates", "uae", "emirates")
_nat("usa", "usa", "america")
_nat("united states", "usa", "america")
_nat("united states of america", "usa", "america")
_nat("scotland", "scotland", "sco")
_nat("netherlands", "netherlands", "ned", "holland")
_nat("argentina", "argentina", "arg")
_nat("brazil", "brazil", "bra")
_nat("france", "france", "fra")
_nat("germany", "germany", "ger", "deu")
_nat("spain", "spain", "esp")
_nat("portugal", "portugal", "por")
_nat("italy", "italy", "ita")
_nat("japan", "japan", "jpn")
_nat("south korea", "korea", "kor")
_nat("iran", "iran", "irn")

# Words that carry no club identity on their own.
_CLUB_STOPWORDS = {
    "football", "club", "cricket", "fc", "sc", "cf", "afc", "the",
    "city", "united", "town", "county", "athletic", "albion",
    "wanderers", "rovers", "hotspur", "women", "woman", "ladies",
    "youth", "reserves",
}

_LEAGUE_STOPWORDS = {"the", "and", "of"}


def team_tokens(name):
    """Tokens that identify a team inside a channel name: the
    alias set for a national side, else the club name's
    distinctive words."""
    key = norm_key(name)
    if key in NATIONAL_TOKENS:
        return set(NATIONAL_TOKENS[key])
    return {t for t in _tokens(name)
            if len(t) >= 4 and t not in _CLUB_STOPWORDS}


def league_tokens(name):
    return {t for t in _tokens(name)
            if len(t) >= 4 and t not in _LEAGUE_STOPWORDS}


def build_channel_index(by_key):
    """[(display, token set)] for every distinct playlist channel."""
    index = []
    seen = set()
    for display in by_key.values():
        if display in seen:
            continue
        seen.add(display)
        index.append((display, _tokens(display)))
    return index


# Distinctive competition brands for dedicated-channel spotting:
# (sports, league keywords, token sets — any one fully contained
# in a channel's name marks that channel as dedicated to the
# competition).
DEDICATED_LEAGUE_TOKENS = (
    ({"Cricket"}, ("indian premier league",), ({"ipl"},)),
    ({"Football"}, ("la liga", "laliga"), ({"laliga"}, {"la", "liga"})),
    ({"Football"}, ("world cup",), ({"world", "cup"},)),
)

_DEDICATED_MARKERS = {"live", "event", "events"}


def _pass_a(ev, index):
    """All dedicated event channels, most specific match first."""
    toks_a = team_tokens(ev["teamA"])
    toks_b = team_tokens(ev["teamB"])
    lg = league_tokens(ev["league"])
    league_key = norm_key(ev["league"])
    sport_word = norm_key(ev["sport"])
    dedicated = [token_sets for sports, kws, token_sets
                 in DEDICATED_LEAGUE_TOKENS
                 if ev["sport"] in sports
                 and any(k in league_key for k in kws)]
    ranked = []
    for display, ctoks in index:
        hit_a = toks_a & ctoks
        hit_b = toks_b & ctoks
        tier, spec = 0, 0
        if hit_a and hit_b:
            # Both teams named in the channel.
            tier, spec = 3, len(hit_a) + len(hit_b)
        elif (hit_a or hit_b) and lg & ctoks:
            # One team + the competition named in the channel.
            tier, spec = 2, len(hit_a) + len(hit_b) + len(lg & ctoks)
        elif any(ts <= ctoks for token_sets in dedicated
                 for ts in token_sets):
            # Channel dedicated to this competition. A branded
            # one ("DAZN LaLiga") beats a bare league-name entry
            # ("la liga"): spec rewards tokens beyond the brand.
            tier = 1
            extra = min(len(ctoks - ts) for token_sets in dedicated
                        for ts in token_sets if ts <= ctoks)
            spec = 2 if extra else 1
        if not tier:
            continue
        rank = (tier, spec,
                1 if _DEDICATED_MARKERS & ctoks else 0,
                1 if sport_word in ctoks else 0,
                -len(ctoks), display)
        ranked.append((rank, display))
    ranked.sort(reverse=True)
    return [display for _rank, display in ranked]


# (sports, league keywords, either-team keywords, candidate names
# in flagship order). A league keyword matches when it is a
# substring of the normalized league name OR one whole league
# token (so "nba" hits the league "NBA" but not "WNBA"). Every
# candidate is verified against the corpus at runtime.
BROADCASTER_RULES = (
    # --- Cricket ---
    ({"Cricket"}, (), ("bangladesh",),
     ("Gazi TV", "T Sports", "Nagorik TV", "T Sports HD",
      "Gazi TV HD")),
    ({"Cricket"}, ("indian premier league",), (),
     ("Star Sports 1 HD", "Star Sports 2 HD")),
    ({"Cricket"}, ("pakistan super league",), (),
     ("A Sports HD", "PTV Sports HD", "A Sports", "PTV Sports")),
    ({"Cricket"}, ("bangladesh premier league",), (),
     ("Gazi TV", "T Sports", "Nagorik TV")),
    ({"Cricket"}, (), ("pakistan",),
     ("PTV Sports HD", "PTV Sports")),
    ({"Cricket"}, ("asia cup",), (),
     ("Star Sports 1 HD", "T Sports")),
    ({"Cricket"}, (), ("india",),
     ("Star Sports 1 HD",)),
    ({"Cricket"}, ("icc", "world cup", "champions trophy",
                   "world test championship"), (),
     ("Star Sports 1 HD", "T Sports", "PTV Sports HD")),
    ({"Cricket"}, ("big bash",), (),
     ("Star Sports 1 HD", "Star Sports 2 HD")),
    # --- Football ---
    ({"Football"}, ("english premier league",), (),
     ("STAR SPORTS 1 SELECT HD", "Star Sports Select 1",
      "Sky Sports", "TNT Sport 1", "BEIN Sports 1")),
    ({"Football"}, ("uefa champions league",), (),
     ("Sony Ten Sports 1 HD", "Sony Ten 1 HD", "Ten Sports",
      "BEIN Sports 1", "beIN SPORTS XTRA")),
    ({"Football"}, ("la liga", "laliga"), (),
     ("BEIN Sports 1", "BEIN SPORTS 2 HD", "Bein Sports",
      "beIN SPORTS XTRA")),
    ({"Football"}, ("italian serie a",), (),
     ("BEIN Sports 1", "BEIN SPORTS 2 HD", "Bein Sports",
      "beIN SPORTS XTRA")),
    ({"Football"}, ("german bundesliga",), (),
     ("Sony Ten Sports 2 HD", "Sony Ten 2 HD", "Sony Sports Ten 2",
      "BEIN Sports 1")),
    ({"Football"}, ("french ligue 1",), (),
     ("BEIN Sports 1", "BEIN SPORTS 2 HD", "Bein Sports",
      "beIN SPORTS XTRA")),
    ({"Football"}, ("fa cup",), (),
     ("Sony Ten Sports 1 HD", "Sony Ten 1 HD")),
    ({"Football"}, (), ("bangladesh",),
     ("T Sports", "T Sports HD")),
    # --- Basketball ---
    ({"Basketball"}, ("national basketball association", "nba"), (),
     ("NBA",)),
    # Only the AUSTRALIAN NBL (ESPN carries it) — Bulgaria / Czech
    # leagues brand themselves "NBL" too and must stay unmapped.
    ({"Basketball"}, ("australian nbl", "nbl australia"), (),
     ("ESPN", "ESPN 2")),
    # --- Tennis: the Grand Slams live on Eurosport in this corpus ---
    ({"Tennis"}, ("wimbledon", "roland garros", "french open",
                  "australian open", "us open"), (),
     ("Eurosport HD", "Eurosport")),
)


def _pass_b(ev, by_key):
    """League broadcaster table, verified against the corpus.

    Every matching rule contributes ALL of its verified
    candidates, in rule order then flagship order — a rule that
    names two broadcasters (e.g. Bangladesh cricket -> T Sports /
    Gazi TV) yields both."""
    league = norm_key(ev["league"])
    lg_toks = _tokens(ev["league"])
    teams = (norm_key(ev["teamA"]), norm_key(ev["teamB"]))
    out = []
    for sports, league_kws, team_kws, candidates in BROADCASTER_RULES:
        if ev["sport"] not in sports:
            continue
        if league_kws and not any(k in league or k in lg_toks
                                  for k in league_kws):
            continue
        if team_kws and not any(k in t for k in team_kws
                                for t in teams):
            continue
        for cand in candidates:
            hit = by_key.get(norm_key(cand))
            if hit and hit not in out:
                out.append(hit)
    return out


MAX_CHANNELS = 5


def map_channels(ev, by_key, index, health=None, app_keys=None):
    """Returns (channels, pass) — up to MAX_CHANNELS verified
    channel names in priority order: every dedicated-channel
    match (pass A) first, then the broadcaster table's entries
    (pass B), deduplicated. pass is "A" when any dedicated
    channel matched, else "B" when the table matched, else ""
    (unmapped).

    When [health] (from load_channel_health) is given, the
    ordered list is stable-partitioned so channels with at least
    one not-proven-dead stream come before channels whose every
    stream the probe killed — a dead flagship (e.g. a Select
    channel whose only server 404s) must not headline an event
    while a working alternative sits in the chips. All-dead
    channels are then dropped when any alternative survives;
    only when every candidate is all-dead is the list kept as
    mapped (the probe runs from a foreign network, so its
    verdict is a hint, not a sentence)."""
    a_hits = _pass_a(ev, index)
    b_hits = _pass_b(ev, by_key)
    channels = []
    for hit in a_hits + b_hits:
        if hit not in channels:
            channels.append(hit)
    if health or app_keys:
        def rank(c):
            corpus_only = 1 if (app_keys is not None
                                and norm_key(c) not in app_keys) else 0
            all_dead = 0 if (not health
                             or health.get(norm_key(c), True)) else 1
            return (corpus_only, all_dead)
        channels = sorted(channels, key=rank)  # stable: A/B order kept
    if health:
        # Drop channels the probe proved dead on every server
        # when a not-proven-dead alternative remains — a chip
        # that cannot play is worse than no chip. When every
        # candidate is all-dead the list is kept as mapped: the
        # probe runs from a foreign network, so its verdict is
        # a hint, and stripping the card's channels entirely
        # would hide the fixture's nominal carrier for nothing.
        alive = [c for c in channels
                 if health.get(norm_key(c), True)]
        if alive:
            channels = alive
    channels = channels[:MAX_CHANNELS]
    if a_hits:
        primary = "A"
    elif b_hits:
        primary = "B"
    else:
        primary = ""
    return channels, primary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _priority(ev):
    score = 0
    if ev.get("channel"):
        score += 4
    if ev["sport"] == "Cricket":
        score += 2
    league = norm_key(ev["league"])
    if any(h in league for h in MAJOR_LEAGUE_HINTS):
        score += 1
    if ev.get("flagA") and ev.get("flagB"):
        score += 1
    return score


def main():
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = today - timedelta(days=1)
    window_end = today + timedelta(days=4)  # exclusive: +3 days fully
    days = [today + timedelta(days=n) for n in range(-1, 4)]

    print("%s: fetching fixtures %s .. %s"
          % (GENERATOR, window_start.strftime("%Y-%m-%d"),
             (window_end - timedelta(days=1)).strftime("%Y-%m-%d")),
          flush=True)

    raw = []
    sources_ok = 0
    for fetcher in (fetch_tsdb, fetch_espn):
        try:
            events, ok = fetcher(days)
        except Exception as e:  # noqa: BLE001
            print("  %s failed: %s" % (fetcher.__name__, e), flush=True)
            events, ok = [], 0
        raw.extend(events)
        sources_ok += ok

    if sources_ok == 0 or not raw:
        print("ERROR: no source produced any event — leaving %s "
              "untouched" % OUTPUT_PATH, flush=True)
        return 1

    by_key = load_channel_names(APP_DIR)
    index = build_channel_index(by_key)
    print("  %d channel name(s) in the merged playlists"
          % len(by_key), flush=True)
    health = load_channel_health(APP_DIR)
    if health:
        print("  probe health: %d channel(s) with a live server, "
              "%d all-dead (demoted in mapping)"
              % (sum(1 for v in health.values() if v),
                 sum(1 for v in health.values() if not v)),
              flush=True)
    app_keys = load_app_channel_keys(APP_DIR)
    print("  %d channel(s) carried by the app's own playlists"
          % len(app_keys), flush=True)
    free_pool = load_free_pool(APP_DIR)
    print("  free corpus pool: %d channel(s) under data/"
          % len(free_pool), flush=True)

    # Attach flags + channel, then dedupe across sources.
    best = {}
    for ev in raw:
        if not (window_start <= ev["start"] < window_end):
            continue
        ev["flagA"] = flag_for(ev["teamA"], ev["badgeA"])
        ev["flagB"] = flag_for(ev["teamB"], ev["badgeB"])
        ev["channels"], ev["mpass"] = map_channels(ev, by_key, index,
                                                  health, app_keys)
        ev["channel"] = ev["channels"][0] if ev["channels"] else ""
        key = (ev["sport"], _team_key(ev["teamA"]),
               _team_key(ev["teamB"]), ev["start"].strftime("%Y-%m-%d"))
        rank = (_priority(ev), 1 if ev["source"] == "tsdb" else 0)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, ev)
    events = [ev for _rank, ev in best.values()]
    print("  %d unique event(s) in window after dedupe" % len(events),
          flush=True)

    # Hard guarantee for the owner's cricket+football-only rule:
    # even if a source starts returning another sport, it never
    # reaches the file.
    events = [ev for ev in events if ev["sport"] in ALLOWED_SPORTS]

    if len(events) > MAX_EVENTS:
        events.sort(key=lambda e: (-_priority(e), e["start"]))
        events = events[:MAX_EVENTS]
        print("  capped to %d event(s) by priority" % MAX_EVENTS,
              flush=True)
    events.sort(key=lambda e: e["start"])

    # Free fallback (owner's rule): events whose primary channel
    # the app does not carry get probed free-corpus URLs, so the
    # app can still play them. Runs on robot-mapped events only —
    # manual fixtures are merged later and never pass through here.
    free_attached = attach_free_urls(events, app_keys, free_pool)
    if free_attached:
        print("  free fallback: %d event(s) got free stream URLs"
              % free_attached, flush=True)

    used_ids = set()
    out_events = []
    for ev in events:
        base = "%s-%s-%s" % (_slug(ev["teamA"]), _slug(ev["teamB"]),
                             ev["start"].strftime("%Y-%m-%d"))
        slug_id, n = base, 2
        while slug_id in used_ids:
            slug_id = "%s-%d" % (base, n)
            n += 1
        used_ids.add(slug_id)
        entry = {
            "id": slug_id,
            "sport": ev["sport"],
            "league": ev["league"],
            "teamA": ev["teamA"],
            "teamB": ev["teamB"],
            "flagA": ev["flagA"],
            "flagB": ev["flagB"],
            "startUtc": ev["start"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channel": ev["channel"],
            "channels": ev["channels"],
        }
        if ev.get("freeUrls"):
            entry["freeUrls"] = ev["freeUrls"]
        out_events.append(entry)

    # Manual fixtures (app/manual_events.json): hand-added matches
    # the free sources miss (e.g. a tri-series TheSportsDB does not
    # carry). Same schema as the output events; a manual entry
    # replaces a fetched one with the same id, and entries outside
    # the window or outside the allowed sports are ignored.
    manual_path = APP_DIR / "manual_events.json"
    if manual_path.exists():
        try:
            manual = json.loads(
                manual_path.read_text(encoding="utf-8"))
            kept = []
            for entry in manual.get("events") or []:
                start = _parse_espn_time(entry.get("startUtc"))
                if (entry.get("sport") in ALLOWED_SPORTS
                        and start is not None
                        and window_start <= start < window_end
                        and entry.get("id")
                        and entry.get("teamA") and entry.get("teamB")):
                    kept.append({
                        "id": entry["id"],
                        "sport": entry["sport"],
                        "league": entry.get("league", ""),
                        "teamA": entry["teamA"],
                        "teamB": entry["teamB"],
                        "flagA": entry.get("flagA", ""),
                        "flagB": entry.get("flagB", ""),
                        "startUtc": start.strftime(
                            "%Y-%m-%dT%H:%M:%SZ"),
                        "channel": entry.get("channel", ""),
                        "channels": ([entry["channel"]]
                                     if entry.get("channel")
                                     else []),
                    })
            if kept:
                manual_ids = {e["id"] for e in kept}
                out_events = [e for e in out_events
                              if e["id"] not in manual_ids] + kept
                out_events.sort(key=lambda e: e["startUtc"])
                print("  manual fixtures merged: %d" % len(kept),
                      flush=True)
        except Exception as e:  # noqa: BLE001
            print("  manual fixtures skipped (%s)" % e, flush=True)

    report = {
        "updatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": GENERATOR,
        "events": out_events,
    }
    OUTPUT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False,
                   sort_keys=True) + "\n",
        encoding="utf-8")

    per_sport = {}
    for ev in out_events:
        per_sport[ev["sport"]] = per_sport.get(ev["sport"], 0) + 1
    mapped = sum(1 for ev in out_events if ev["channel"])
    multi = sum(1 for ev in out_events if len(ev["channels"]) >= 2)
    with_free = sum(1 for ev in out_events if ev.get("freeUrls"))
    in_pass = {"A": 0, "B": 0}
    for ev in events:
        if ev.get("mpass") in in_pass:
            in_pass[ev["mpass"]] += 1
    print("Wrote %s" % OUTPUT_PATH)
    print("  total: %d event(s); with channel: %d "
          "(pass A: %d, pass B: %d); unmapped: %d; "
          "with 2+ channels: %d; with free fallback: %d"
          % (len(out_events), mapped, in_pass["A"], in_pass["B"],
             len(out_events) - mapped, multi, with_free))
    for sport in sorted(per_sport):
        print("    %s: %d" % (sport, per_sport[sport]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
