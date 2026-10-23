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

Channel links are CONSERVATIVE: a small rule table maps a league /
team to a candidate channel name, and the mapping is applied only
when that exact name exists in the merged playlists (app/*.m3u).
An event without a verified channel stays info-only in the app.

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

# TSDB sport name -> app sport name ("Soccer" is shown as Football;
# TSDB's "Fighting" covers MMA cards).
TSDB_DAY_SPORTS = ("Cricket", "Soccer", "Basketball", "Tennis",
                   "Fighting")
TSDB_LIVE_SPORTS = ("Cricket", "Soccer", "Basketball")
SPORT_MAP = {"Soccer": "Football", "Fighting": "MMA"}

# ESPN league paths -> app sport. Football majors + NBA + IPL.
ESPN_LEAGUES = (
    ("soccer/eng.1", "Football"),
    ("soccer/esp.1", "Football"),
    ("soccer/ita.1", "Football"),
    ("soccer/ger.1", "Football"),
    ("soccer/fra.1", "Football"),
    ("soccer/uefa.champions", "Football"),
    ("basketball/nba", "Basketball"),
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
# Channel mapping — conservative; a candidate is used ONLY when its
# normalized name exists among the merged playlists' channel names.
# The emitted value is the playlist's own display form.
# ---------------------------------------------------------------------------

# (sports, league keywords, either-team keywords, candidate names)
CHANNEL_RULES = (
    ({"Cricket"}, (), ("bangladesh",),
     ("T Sports", "Gazi TV", "T Sports HD", "Gazi TV HD")),
    ({"Cricket"}, ("indian premier league",), (),
     ("Star Sports 1 HD",)),
    ({"Cricket"}, ("pakistan super league",), (),
     ("A Sports HD", "PTV Sports")),
    ({"Cricket"}, (), ("pakistan",),
     ("PTV Sports", "PTV Sports HD")),
    ({"Cricket"}, ("asia cup",), (),
     ("Star Sports 1 HD", "T Sports")),
    ({"Cricket"}, (), ("india",),
     ("Star Sports 1 HD",)),
    ({"Cricket"}, ("icc", "world cup", "champions trophy"), (),
     ("Star Sports 1 HD", "T Sports")),
    ({"Football"}, ("english premier league",), (),
     ("Star Sports Select 1 HD", "STAR SPORTS 1 SELECT HD",
      "Star Sports Select 1")),
    ({"Football"}, ("uefa champions league",), (),
     ("Sony Ten Sports 1 HD", "Ten Sports")),
    ({"Football"}, (), ("bangladesh",),
     ("T Sports",)),
)


def load_channel_names(app_dir):
    """norm_key(cleaned name) -> display name, from every app/*.m3u."""
    by_key = {}
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
                    by_key.setdefault(norm_key(cleaned), cleaned)
    return by_key


def map_channel(ev, by_key):
    league = norm_key(ev["league"])
    teams = (norm_key(ev["teamA"]), norm_key(ev["teamB"]))
    for sports, league_kws, team_kws, candidates in CHANNEL_RULES:
        if ev["sport"] not in sports:
            continue
        if league_kws and not any(k in league for k in league_kws):
            continue
        if team_kws and not any(k in t for k in team_kws for t in teams):
            continue
        for cand in candidates:
            hit = by_key.get(norm_key(cand))
            if hit:
                return hit
    return ""


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
    print("  %d channel name(s) in the merged playlists"
          % len(by_key), flush=True)

    # Attach flags + channel, then dedupe across sources.
    best = {}
    for ev in raw:
        if not (window_start <= ev["start"] < window_end):
            continue
        ev["flagA"] = flag_for(ev["teamA"], ev["badgeA"])
        ev["flagB"] = flag_for(ev["teamB"], ev["badgeB"])
        ev["channel"] = map_channel(ev, by_key)
        key = (ev["sport"], _team_key(ev["teamA"]),
               _team_key(ev["teamB"]), ev["start"].strftime("%Y-%m-%d"))
        rank = (_priority(ev), 1 if ev["source"] == "tsdb" else 0)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, ev)
    events = [ev for _rank, ev in best.values()]
    print("  %d unique event(s) in window after dedupe" % len(events),
          flush=True)

    if len(events) > MAX_EVENTS:
        events.sort(key=lambda e: (-_priority(e), e["start"]))
        events = events[:MAX_EVENTS]
        print("  capped to %d event(s) by priority" % MAX_EVENTS,
              flush=True)
    events.sort(key=lambda e: e["start"])

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
        out_events.append({
            "id": slug_id,
            "sport": ev["sport"],
            "league": ev["league"],
            "teamA": ev["teamA"],
            "teamB": ev["teamB"],
            "flagA": ev["flagA"],
            "flagB": ev["flagB"],
            "startUtc": ev["start"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channel": ev["channel"],
        })

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
    print("Wrote %s" % OUTPUT_PATH)
    print("  total: %d event(s); with channel: %d"
          % (len(out_events), mapped))
    for sport in sorted(per_sport):
        print("    %s: %d" % (sport, per_sport[sport]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
