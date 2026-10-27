#!/usr/bin/env python3
"""Regression tests for cross-source duplicate merging.

Run: python3 scripts/test_dedupe.py
Exits non-zero if any case fails.
"""

import contextlib
import io
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_events as fe  # noqa: E402

FAILURES = []


def ev(team_a, team_b, league, source, start, sport="Football"):
    return {
        "sport": sport,
        "league": league,
        "teamA": team_a,
        "teamB": team_b,
        "start": start,
        "badgeA": "",
        "badgeB": "",
        "source": source,
        "channels": [],
        "channel": "",
        "flagA": "",
        "flagB": "",
    }


def merged(events):
    with contextlib.redirect_stdout(io.StringIO()):
        return fe.merge_duplicates(deepcopy(events))


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print("%s: %s%s" % (status, name,
                        (" — " + detail) if detail else ""))
    if not condition:
        FAILURES.append(name)


BASE = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)


def test_canon_aliases():
    expected = {
        "AS Roma": "roma",
        "Roma": "roma",
        "Internazionale": "inter",
        "Inter Milan": "inter",
        "Inter": "inter",
        "AC Milan": "milan",
        "Milan": "milan",
        "SSC Napoli": "napoli",
        "Napoli": "napoli",
        "Genoa CFC": "genoa",
        "Genoa": "genoa",
        "SE Palmeiras": "palmeiras",
        "Palmeiras": "palmeiras",
        "RB Bragantino": "bragantino",
        "Bragantino": "bragantino",
        "Chapecoense AF": "chapecoense",
        "Chapecoense": "chapecoense",
    }
    bad = [(name, fe._canon_team(name), want)
           for name, want in expected.items()
           if fe._canon_team(name) != want]
    check("canon aliases", not bad, "mismatches: %r" % (bad,))


def test_eleven_fixtures():
    serie_a = "Serie A"
    italian = "Italian Serie A"
    br_sd = "Campeonato Brasileiro Série A"
    br_tsdb = "Brazilian Serie A"
    rows = [
        # (sportsdata A, sportsdata B, other A, other B, other league,
        #  other source, variant offset minutes)
        ("Sassuolo", "Lazio", "Sassuolo", "Lazio", italian, "espn", 0),
        ("AS Roma", "Cagliari", "AS Roma", "Cagliari", italian,
         "espn", 30),
        ("Torino", "Como", "Torino", "Como", italian, "espn", 90),
        ("Internazionale", "Venezia", "Internazionale", "Venezia",
         italian, "espn", 0),
        ("AC Milan", "Bologna", "AC Milan", "Bologna", italian,
         "espn", 30),
        ("Monza", "Napoli", "Monza", "Napoli", italian, "espn", 90),
        ("Genoa", "Juventus", "Genoa", "Juventus", italian, "espn", 0),
        ("Parma", "Udinese", "Parma", "Udinese", italian, "espn", 30),
        ("Santos", "Palmeiras", "Santos", "Palmeiras", br_tsdb,
         "tsdb", 90),
        ("Bragantino", "Chapecoense", "Bragantino", "Chapecoense",
         br_tsdb, "tsdb", 0),
        ("Fluminense", "Internacional", "Fluminense", "Internacional",
         br_tsdb, "tsdb", 30),
    ]
    events = []
    for i, (sa, sb, oa, ob, league, source, off) in enumerate(rows):
        start = BASE + timedelta(minutes=30 * i)
        sd_league = serie_a if i < 8 else br_sd
        events.append(ev(sa, sb, sd_league, "sportsdata", start))
        events.append(ev(oa, ob, league, source,
                         start + timedelta(minutes=off)))
    # Third copy of the Inter fixture under TSDB's name form.
    inter_start = BASE + timedelta(minutes=90)
    events.append(ev("Inter Milan", "Venezia", italian, "tsdb",
                     inter_start + timedelta(minutes=90)))
    out = merged(events)
    check("11 sportsdata fixtures merge to 11", len(out) == 11,
          "unique=%d from %d inputs" % (len(out), len(events)))
    return len(out)


def test_reversed_orientation():
    events = [
        ev("Alpha Rovers", "Beta City", "Test League", "espn", BASE),
        ev("Beta City", "Alpha Rovers", "Test League", "sportsdata",
           BASE + timedelta(minutes=30)),
    ]
    out = merged(events)
    check("reversed home/away merges", len(out) == 1,
          "unique=%d" % len(out))
    return len(out)


def test_different_date_no_merge():
    events = [
        ev("Alpha Rovers", "Beta City", "Test League", "espn", BASE),
        ev("Alpha Rovers", "Beta City", "Test League", "sportsdata",
           BASE + timedelta(days=1)),
    ]
    out = merged(events)
    check("same teams different date stays separate", len(out) == 2,
          "unique=%d" % len(out))
    return len(out)


def test_five_hours_apart_no_merge():
    events = [
        ev("Inter", "Roma", "Serie A", "sportsdata", BASE),
        ev("Internazionale", "AS Roma", "Italian Serie A", "espn",
           BASE + timedelta(hours=5)),
    ]
    out = merged(events)
    check("same teams 5h apart stays separate", len(out) == 2,
          "unique=%d" % len(out))
    return len(out)


def test_incompatible_league_no_merge():
    events = [
        ev("Inter", "AS Roma", "Serie A", "sportsdata", BASE),
        ev("Internazionale", "Roma", "Coppa Italia", "espn", BASE),
    ]
    out = merged(events)
    check("incompatible leagues stay separate", len(out) == 2,
          "unique=%d" % len(out))
    return len(out)


def test_two_hour_boundary_merges():
    events = [
        ev("Inter", "Roma", "Serie A", "sportsdata", BASE),
        ev("Internazionale", "AS Roma", "Italian Serie A", "espn",
           BASE + timedelta(hours=2)),
    ]
    out = merged(events)
    check("exactly 2h apart merges (boundary)", len(out) == 1,
          "unique=%d" % len(out))
    return len(out)


def test_two_hour_plus_no_merge():
    events = [
        ev("Inter", "Roma", "Serie A", "sportsdata", BASE),
        ev("Internazionale", "AS Roma", "Italian Serie A", "espn",
           BASE + timedelta(hours=2, minutes=1)),
    ]
    out = merged(events)
    check("2h1m apart stays separate", len(out) == 2,
          "unique=%d" % len(out))
    return len(out)


def test_missing_start_no_merge():
    events = [
        ev("Inter", "Roma", "Serie A", "sportsdata", None),
        ev("Internazionale", "AS Roma", "Italian Serie A", "espn", BASE),
    ]
    out = merged(events)
    check("missing kickoff never merges", len(out) == 2,
          "unique=%d" % len(out))
    return len(out)


def main():
    test_canon_aliases()
    counts = {
        "eleven_fixtures": test_eleven_fixtures(),
        "reversed": test_reversed_orientation(),
        "different_date": test_different_date_no_merge(),
        "five_hours": test_five_hours_apart_no_merge(),
        "incompatible_league": test_incompatible_league_no_merge(),
        "two_hour_boundary": test_two_hour_boundary_merges(),
        "two_hour_plus": test_two_hour_plus_no_merge(),
        "missing_start": test_missing_start_no_merge(),
    }
    print("SUMMARY unique counts: %s" % counts)
    if FAILURES:
        print("RESULT: FAIL (%d case(s): %s)"
              % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
