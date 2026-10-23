#!/usr/bin/env python3
"""BDlivetv Logo Bot.

Fixes broken or missing channel logos across app/*.m3u:

  1. Every channel's own tvg-logo URL is verified (it must answer
     with an actual image, not a 404 or an HTML page).
  2. Channels whose logo is missing or dead get a replacement looked
     up in the iptv-org database (channels.json + logos.json):
     first by tvg-id, then by channel name. A replacement is only
     used after it verifies as a live image itself.

Output: app/channel_logos.json — FIXES ONLY:
    { "updatedAt", "generator",
      "logos": { "<cleaned channel name>": "<working logo url>" } }

The app uses the playlist's own logo when it loads, falls back to
this file, and draws a placeholder when neither exists — so a broken
logo image is never shown. Raster logos (PNG/JPEG/WebP) are preferred
over SVG, which the player cannot render everywhere.

Stdlib only. Run after categorize_channels.py in the Auto Update
workflow; also runnable on its own:

    python3 scripts/fix_logos.py
"""

import json
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

socket.setdefaulttimeout(30)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from categorize_channels import clean_name, norm_key  # noqa: E402

GENERATOR = "BDlivetv Logo Bot"
REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "app" / "channel_logos.json"

CHANNELS_API = "https://iptv-org.github.io/api/channels.json"
LOGOS_API = "https://iptv-org.github.io/api/logos.json"

UA = "BDlivetvLogoBot/1.0 (https://github.com/ryhanfxbd/ryhanfxbd)"
TIMEOUT = 12
WORKERS = 10
SAMPLE_BYTES = 2048

# ---------------------------------------------------------------------------
# M3U parsing — name, tvg-id and tvg-logo per entry.
# ---------------------------------------------------------------------------

_TVG_ID = re.compile(r'tvg-id="([^"]*)"')
_TVG_LOGO = re.compile(r'tvg-logo="([^"]*)"')


def parse_entries(text):
    entries = []
    name = ""
    tvg_id = ""
    logo = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            m = _TVG_ID.search(line)
            tvg_id = m.group(1).strip() if m else ""
            m = _TVG_LOGO.search(line)
            logo = m.group(1).strip() if m else ""
        elif line.startswith("#"):
            continue
        elif line.lower().startswith(("http://", "https://",
                                      "rtmp://", "rtsp://")):
            entries.append((name, tvg_id, logo))
            name = ""
            tvg_id = ""
            logo = ""
    return entries


# ---------------------------------------------------------------------------
# Image verification — the URL must serve an actual image.
# ---------------------------------------------------------------------------

def _looks_like_image(content_type, sample):
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct.startswith("image/"):
        return True
    if sample.startswith(b"\x89PNG") or sample.startswith(b"\xff\xd8"):
        return True
    if sample.startswith(b"GIF8"):
        return True
    if sample[:4] == b"RIFF" and sample[8:12] == b"WEBP":
        return True
    if sample.startswith(b"\x00\x00\x01\x00"):  # ICO
        return True
    head = sample.lstrip()[:200].lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml")
                                    and b"<svg" in head):
        return True
    return False


def _fetch_sample(url, use_range):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if use_range:
        req.add_header("Range", "bytes=0-%d" % (SAMPLE_BYTES - 1))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        sample = resp.read(SAMPLE_BYTES)
        ctype = resp.headers.get_content_type() if resp.headers else ""
        return resp.getcode() or 200, ctype, sample


def verify_image(url):
    """True when the URL ultimately serves an image (redirects are
    followed by urllib; a friendly 2xx HTML page does NOT count)."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    try:
        code, ctype, sample = _fetch_sample(url, use_range=True)
    except urllib.error.HTTPError as e:
        if e.code in (405, 416):
            try:
                code, ctype, sample = _fetch_sample(url, use_range=False)
            except Exception:
                return False
        else:
            return False
    except Exception:
        return False
    if not (200 <= code < 300):
        return False
    return _looks_like_image(ctype, sample)


# ---------------------------------------------------------------------------
# iptv-org index: channel name / tvg-id -> best raster logo URL.
# ---------------------------------------------------------------------------

_STOP_TOKENS = {"tv", "hd", "sd", "fhd", "uhd", "4k", "channel",
                "live", "the", "and", "plus", "+", "bd"}


def _tokens(s):
    return {t for t in re.findall(r"[a-z0-9]+", s.lower())
            if t not in _STOP_TOKENS and len(t) > 1}


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def build_iptv_index():
    """Returns (by_id, by_name): id.lower() -> logo url, and
    norm_key(name) -> logo url. Prefers in-use raster logos, largest
    first; SVG only when nothing else exists for that channel."""
    channels = _get_json(CHANNELS_API)
    logos = _get_json(LOGOS_API)
    best = {}  # channel id -> (score, url)
    for entry in logos:
        cid = (entry.get("channel") or "").strip()
        url = (entry.get("url") or "").strip()
        if not cid or not url:
            continue
        fmt = (entry.get("format") or "").upper()
        raster = fmt in ("PNG", "JPG", "JPEG", "WEBP")
        area = (entry.get("width") or 0) * (entry.get("height") or 0)
        score = (1 if entry.get("in_use") else 0,
                 1 if raster else 0, area)
        if cid not in best or score > best[cid][0]:
            best[cid] = (score, url, raster)
    by_id = {}
    by_name = {}
    for ch in channels:
        cid = (ch.get("id") or "").strip()
        if cid not in best:
            continue
        _score, url, raster = best[cid]
        if not raster:
            continue  # SVG-only: the player cannot render it reliably
        by_id[cid.lower()] = url
        names = [ch.get("name") or ""] + (ch.get("alt_names") or [])
        for nm in names:
            key = norm_key(clean_name(nm))
            if key:
                by_name.setdefault(key, url)
    return by_id, by_name


_QUALITY_TOKEN = re.compile(
    r"[\s\+\-_]*(?:fhd|uhd|hd|sd|4k|8k)\s*$", re.IGNORECASE)


def _variant_base(name):
    out = name.strip()
    while True:
        stripped = _QUALITY_TOKEN.sub("", out).strip(" +-")
        if stripped == out or not stripped:
            return out
        out = stripped


def lookup_logo(name, tvg_ids, by_id, by_name):
    """Best iptv-org logo URL for a channel, unverified."""
    for tid in tvg_ids:
        if tid and tid.lower() in by_id:
            return by_id[tid.lower()]
    key = norm_key(name)
    if key in by_name:
        return by_name[key]
    base_key = norm_key(_variant_base(name))
    if base_key in by_name:
        return by_name[base_key]
    # Fuzzy: an index name whose distinctive tokens cover ours (or
    # the reverse) with at least two shared tokens.
    name_tokens = _tokens(name)
    if not name_tokens:
        return None
    best_url = None
    best_overlap = 1
    for idx_key, url in by_name.items():
        idx_tokens = _tokens(idx_key)
        overlap = len(name_tokens & idx_tokens)
        if overlap > best_overlap and (
                name_tokens <= idx_tokens or idx_tokens <= name_tokens):
            best_overlap = overlap
            best_url = url
    return best_url


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    app_dir = REPO_ROOT / "app"
    playlist_files = sorted(app_dir.glob("*.m3u"),
                            key=lambda p: p.name.lower())
    print("%s: found %d playlist(s) in app/" % (GENERATOR,
                                               len(playlist_files)),
          flush=True)

    # cleaned name -> {"logos": set(), "ids": set()}
    channels = {}
    for path in playlist_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for raw_name, tvg_id, logo in parse_entries(text):
            cleaned = clean_name(raw_name)
            if not cleaned:
                continue
            slot = channels.setdefault(
                norm_key(cleaned),
                {"name": cleaned, "logos": set(), "ids": set()})
            if logo:
                slot["logos"].add(logo)
            if tvg_id:
                slot["ids"].add(tvg_id)
    print("  %d unique channel name(s)" % len(channels), flush=True)

    # Verify every distinct logo URL already in the playlists, once.
    own_urls = set()
    for slot in channels.values():
        own_urls |= slot["logos"]
    print("  verifying %d existing logo URL(s)..." % len(own_urls),
          flush=True)
    own_ok = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(verify_image, u): u for u in own_urls}
        for fut in as_completed(futures):
            own_ok[futures[fut]] = bool(fut.result())

    need_fix = {}
    already_ok = 0
    for key, slot in channels.items():
        if any(own_ok.get(u) for u in slot["logos"]):
            already_ok += 1
        else:
            need_fix[key] = slot
    print("  %d channel(s) already have a working logo; %d need a fix"
          % (already_ok, len(need_fix)), flush=True)
    if not need_fix:
        fixes = {}
    else:
        print("  loading iptv-org logo database...", flush=True)
        by_id, by_name = build_iptv_index()
        print("  index: %d ids, %d names" % (len(by_id), len(by_name)),
              flush=True)
        candidates = {}
        for key, slot in need_fix.items():
            url = lookup_logo(slot["name"], sorted(slot["ids"]),
                              by_id, by_name)
            if url:
                candidates[key] = url
        print("  %d candidate replacement(s) to verify"
              % len(candidates), flush=True)
        cand_urls = set(candidates.values())
        cand_ok = {}
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(verify_image, u): u
                       for u in cand_urls}
            for fut in as_completed(futures):
                cand_ok[futures[fut]] = bool(fut.result())
        fixes = {}
        for key, url in candidates.items():
            if cand_ok.get(url):
                fixes[need_fix[key]["name"]] = url
    print("  fixed %d logo(s); %d channel(s) still without a verified "
          "logo" % (len(fixes), len(need_fix) - len(fixes)), flush=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "updatedAt": now,
        "generator": GENERATOR,
        "logos": dict(sorted(fixes.items(),
                             key=lambda kv: kv[0].lower())),
    }
    OUTPUT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print("Wrote %s" % OUTPUT_PATH)


if __name__ == "__main__":
    main()
