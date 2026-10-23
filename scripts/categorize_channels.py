#!/usr/bin/env python3
"""BDlivetv Category Bot.

Assigns every channel in app/*.m3u to ONE of the fixed categories:

    Bangla, News, Sports, Entertainment, Movies, Islamic, Kids, Music

How a channel gets its category (first hit wins):

  1. Cache — a name already classified in app/channel_categories.json
     keeps its category, so each run only researches NEW channels.
  2. Name rules — clear genre words inside the channel name itself
     ("News", "Sports", "Cinema", "Cartoon", "Music", "Islamic", ...).
  3. Web research — the name is looked up on Wikipedia (the channel's
     own article description + first sentence: "Bangladeshi news
     channel", "children's television channel", "Islamic television
     network", ...) and the genre words there decide. A result is
     only trusted when its title really matches the channel name.
  4. Playlist group — the group-title the channel came with, mapped
     onto the fixed set.
  5. Default — "Entertainment".

After classification, variants of one channel that differ only by a
quality suffix (Somoy TV / Somoy TV HD) are unified into one category.

Output: app/channel_categories.json
    { "updatedAt", "generator", "categories": [...fixed list...],
      "channels": { "<cleaned channel name>": "<category>", ... } }

The app reads that file the same way it reads app/playlists.json;
admin overrides made inside the app still win over the bot.

Stdlib only. Invoked by the BDlivetv Auto Update workflow (via
refresh_playlists.py) and runnable on its own:

    python3 scripts/categorize_channels.py [--all]

--all ignores the cache and re-researches every channel.
"""

import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

socket.setdefaulttimeout(30)

GENERATOR = "BDlivetv Category Bot"
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "app" / "channel_categories.json"

FIXED_CATEGORIES = [
    "Bangla", "News", "Sports", "Entertainment",
    "Movies", "Islamic", "Kids", "Music",
]

UA = "BDlivetvCategoryBot/1.0 (https://github.com/ryhanfxbd/ryhanfxbd)"
WIKI_API = "https://en.wikipedia.org/w/api.php"
WORKERS = 6
TIMEOUT = 15

# ---------------------------------------------------------------------------
# Name handling — mirrors the app: only trailing resolution-number tokens
# (720p, 1080p, ...) are stripped; HD/SD/FHD/UHD/4K are part of a channel's
# identity and are KEPT.
# ---------------------------------------------------------------------------

_RES_TOKEN = re.compile(
    r"[\s\(\[]*(?:2160|1440|1080|720|540|480|360|240)p?[\s\)\]]*$",
    re.IGNORECASE)


# Leading serial numbers from playlist numbering ("342 Rplus News",
# "101. BTV World") are list furniture, not part of the channel name.
# Only a STANDALONE digit token followed by a separator/space is
# stripped, so fused brand numbers survive ("9X Jalwa", "TV9 News").
_SERIAL_TOKEN = re.compile(r"^\d{1,4}\s*[\.\)\|\:\-\u2013]?\s+")


def clean_name(name):
    out = (name or "").strip()
    out = _SERIAL_TOKEN.sub("", out).strip()
    while True:
        stripped = _RES_TOKEN.sub("", out).strip()
        if stripped == out:
            return re.sub(r"\s+", " ", out)
        out = stripped


def norm_key(name):
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


# ---------------------------------------------------------------------------
# M3U parsing (name + group-title per entry)
# ---------------------------------------------------------------------------

_GROUP_RE = re.compile(r'group-title="([^"]*)"')


def parse_entries(text):
    entries = []
    name = ""
    group = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            m = _GROUP_RE.search(line)
            group = m.group(1).strip() if m else ""
        elif line.startswith("#"):
            continue
        elif line.lower().startswith(("http://", "https://",
                                      "rtmp://", "rtsp://")):
            entries.append((name, group))
            name = ""
            group = ""
    return entries


# ---------------------------------------------------------------------------
# Genre rules — shared by the name pass and the group pass.
# Order matters: specific genres before the generic buckets.
# ---------------------------------------------------------------------------

_GENRE_RULES = [
    ("Islamic", (r"\bislam", r"\bmuslim\b", r"\bquran", r"\bdeen\b",
                 r"\bwaz\b", r"\bmadani\b", r"\biqra\b", r"\bhuda\b",
                 r"\bpeace tv\b", r"ইসলাম")),
    ("News", (r"\bnews\b", r"খবর", r"সংবাদ", r"\bkhabar\b",
              r"\bsangbad\b", r"\bheadlines\b")),
    ("Sports", (r"\bsport", r"\bcricket\b", r"খেলা", r"\bkhela\b",
                r"\bfootball\b", r"\bsoccer\b", r"\bwwe\b",
                r"\bten sports\b")),
    ("Kids", (r"\bkids?\b", r"\bcartoon", r"\btoon", r"\bnick\b",
              r"শিশু",
              r"\bchildren", r"\banim(e|ation)\b", r"\bnickelodeon\b",
              r"\bpogo\b", r"\bhungama\b", r"\bdoraemon\b")),
    ("Movies", (r"\bmovie", r"\bcinema\b", r"\bhbo\b", r"\bfilm(s)?\b",
                r"\bfilms\b", r"সিনেমা", r"\bpictures\b",
                r"\bflix\b")),
    ("Music", (r"\bmusic\b", r"\bsong", r"\bgaan\b", r"গান",
               r"\bmelody\b", r"\bmtv\b", r"\b9xm\b", r"\bmastiii\b",
               r"\bradio\b")),
]
_GENRE_RES = [(cat, [re.compile(p, re.IGNORECASE) for p in pats])
              for cat, pats in _GENRE_RULES]


def genre_from_text(text):
    """First fixed genre whose keyword pattern appears in text."""
    if not text:
        return None
    for cat, res in _GENRE_RES:
        for r in res:
            if r.search(text):
                return cat
    return None


def category_from_group(group):
    cat = genre_from_text(group)
    if cat:
        return cat
    g = (group or "").lower()
    if "bangla" in g or "bangladesh" in g or "bengali" in g:
        return "Bangla"
    return None


# ---------------------------------------------------------------------------
# Web research — Wikipedia search + article description/first sentence.
# ---------------------------------------------------------------------------

def _wiki_get(params):
    params = dict(params, format="json")
    url = WIKI_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


_STOP_TOKENS = {"tv", "hd", "sd", "fhd", "uhd", "4k", "channel",
                "live", "the", "and", "plus", "+", "bd"}


def _title_matches(channel_name, title):
    """Trust a search result only when its title really is this
    channel: every distinctive name token appears in the title, or at
    least two tokens overlap (guards against near-miss articles like
    "Star Sports" for "Star Plus")."""
    def tokens(s):
        return {t for t in re.findall(r"[a-z0-9]+", s.lower())
                if t not in _STOP_TOKENS and len(t) > 1}
    name_tokens = tokens(channel_name)
    if not name_tokens:
        return False
    title_tokens = tokens(title)
    if name_tokens <= title_tokens:
        return True
    if title_tokens and title_tokens <= name_tokens:
        return True
    return len(name_tokens & title_tokens) >= 2


def _genre_from_wiki_text(text):
    """Genre-scan a Wikipedia description + article lead window.
    The description is a terse genre label, so a bare "news" there is
    decisive; in the lead window only tight genre phrasing counts
    ("news television channel", "sports channel", "Islamic television
    network") - a general channel whose article merely mentions the
    News Corporation or sports coverage must not be misfiled."""
    if not text:
        return None
    desc, _sep, window = text.partition(" ||| ")
    desc = re.sub(r"\s+", " ", desc.lower())
    low = re.sub(r"\s+", " ", (desc + " " + window).lower())
    if re.search(r"islamic (television|channel|network|tv)\b", low) \
            or re.search(r"muslim (television|channel|network)\b", low) \
            or "quran" in low:
        return "Islamic"
    if re.search(r"\bnews\b", desc) \
            or re.search(r"\bnews (channel|television|network|tv)\b",
                         low):
        return "News"
    if re.search(r"\bsports? (channel|television|network|tv)\b", low):
        return "Sports"
    if "children" in low or re.search(r"\bkids\b", low) \
            or "cartoon" in low:
        return "Kids"
    if re.search(r"\bmusic (channel|television|network|tv)\b", low):
        return "Music"
    if re.search(r"\b(film|movie) (channel|television|network)\b", low):
        return "Movies"
    if "bangladeshi" in low or "bengali" in low:
        return "Bangla"
    if "entertainment" in low:
        return "Entertainment"
    return None


def research_category(name):
    """Look the channel up on Wikipedia; return a fixed category or
    None when nothing trustworthy is found."""
    try:
        data = _wiki_get({
            "action": "query", "list": "search",
            "srsearch": name + " television channel",
            "srlimit": 3, "srprop": "snippet",
        })
        hits = data.get("query", {}).get("search", [])
    except Exception:
        return None
    best = None
    for hit in hits:
        if _title_matches(name, hit.get("title", "")):
            best = hit
            break
    if best is None:
        return None
    try:
        data = _wiki_get({
            "action": "query", "prop": "description|extracts",
            "exintro": 1, "explaintext": 1,
            "titles": best["title"],
        })
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            text = (page.get("description", "") + " ||| " +
                    (page.get("extract", "") or "")[:500])
            cat = _genre_from_wiki_text(text)
            if cat:
                return cat
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Variant unification — one channel's HD/SD variants share a category.
# ---------------------------------------------------------------------------

_QUALITY_TOKEN = re.compile(
    r"[\s\+\-_]*(?:fhd|uhd|hd|sd|4k|8k)\s*$", re.IGNORECASE)


def _variant_base(name):
    """Name minus trailing quality tokens (HD/SD/FHD/UHD/4K) - used
    ONLY to keep a channel's own variants in one category."""
    out = name.strip()
    while True:
        stripped = _QUALITY_TOKEN.sub("", out).strip(" +-")
        if stripped == out or not stripped:
            return out
        out = stripped


def unify_variants(result):
    """Channels that differ only by a quality suffix (Somoy TV /
    Somoy TV HD) must share one category: the exact base entry wins,
    else the majority, with specific genres beating generic buckets."""
    groups = {}
    for name, cat in result.items():
        groups.setdefault(norm_key(_variant_base(name)), []).append(
            (name, cat))
    generic = {"Entertainment", "Bangla"}
    fixed = dict(result)
    for base_key, members in groups.items():
        if len(members) < 2:
            continue
        cats = [c for _n, c in members]
        if len(set(cats)) == 1:
            continue
        winner = None
        for name, cat in members:
            if norm_key(name) == base_key:
                winner = cat
                break
        if winner is None:
            specific = [c for c in cats if c not in generic]
            pool = specific or cats
            winner = max(set(pool), key=pool.count)
        for name, _c in members:
            fixed[name] = winner
    return fixed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def classify(name, groups):
    cat = genre_from_text(name)
    if cat:
        return cat, "name"
    cat = research_category(name)
    if cat:
        return cat, "web"
    for group in groups:
        cat = category_from_group(group)
        if cat:
            return cat, "group"
    return "Entertainment", "default"


def main():
    redo_all = "--all" in sys.argv
    app_dir = REPO_ROOT / "app"
    playlist_files = sorted(app_dir.glob("*.m3u"),
                            key=lambda p: p.name.lower())
    print("%s: found %d playlist(s) in app/" % (GENERATOR,
                                               len(playlist_files)),
          flush=True)

    # cleaned display name -> {"key": norm, "groups": set()}
    channels = {}
    for path in playlist_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for raw_name, group in parse_entries(text):
            cleaned = clean_name(raw_name)
            if not cleaned:
                continue
            slot = channels.setdefault(
                norm_key(cleaned), {"name": cleaned, "groups": set()})
            if group:
                slot["groups"].add(group)
    print("  %d unique channel name(s)" % len(channels), flush=True)

    cache = {}
    if OUTPUT_PATH.exists() and not redo_all:
        try:
            old = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            for k, v in (old.get("channels") or {}).items():
                if v in FIXED_CATEGORIES:
                    cache[norm_key(k)] = v
        except Exception:
            cache = {}
    print("  %d channel(s) already classified (cache)" % len(cache),
          flush=True)

    result = {}
    todo = []
    for key, slot in channels.items():
        if key in cache:
            result[slot["name"]] = cache[key]
        else:
            todo.append(slot)
    print("  %d new channel(s) to classify" % len(todo), flush=True)

    if todo:
        done = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {
                pool.submit(classify, slot["name"], sorted(slot["groups"])):
                slot for slot in todo
            }
            for fut in as_completed(futures):
                slot = futures[fut]
                try:
                    cat, _how = fut.result()
                except Exception:
                    cat = "Entertainment"
                result[slot["name"]] = cat
                done += 1
                if done % 100 == 0:
                    print("  classified %d/%d" % (done, len(todo)),
                          flush=True)

    result = unify_variants(result)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "updatedAt": now,
        "generator": GENERATOR,
        "categories": FIXED_CATEGORIES,
        "channels": dict(sorted(result.items(),
                                key=lambda kv: kv[0].lower())),
    }
    OUTPUT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    counts = {}
    for cat in result.values():
        counts[cat] = counts.get(cat, 0) + 1
    print("Wrote %s" % OUTPUT_PATH)
    for cat in FIXED_CATEGORIES:
        print("  %-14s %d" % (cat, counts.get(cat, 0)))


if __name__ == "__main__":
    main()
