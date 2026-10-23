#!/usr/bin/env python3
"""BDlivetv Auto Update - playlist health checker.

Discovers every app/*.m3u playlist in this repository, probes each
unique stream URL, and writes a status report to app/playlist_status.json.
It also writes a playlist manifest to app/playlists.json (every app/*.m3u
except the built-in default channels.m3u) that the BD LIVE TV app syncs
its playlist sources from.

The script NEVER edits or deletes channels in the playlists.
It only writes the status report and the manifest.

Stdlib only (urllib). Runs on GitHub Actions via
.github/workflows/bdlivetv-auto-update.yml
"""

import json
import socket
import subprocess
import sys
import time
import ssl
import urllib.error
import zlib
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if not (REPO_ROOT / "app").is_dir():
    REPO_ROOT = Path.cwd()

STATUS_PATH = REPO_ROOT / "app" / "playlist_status.json"
MANIFEST_PATH = REPO_ROOT / "app" / "playlists.json"
RAW_BASE = "https://raw.githubusercontent.com/ryhanfxbd/ryhanfxbd/main/app/"
BUILTIN_DEFAULT = "channels.m3u"  # the app loads it as its built-in source
GENERATOR = "BDlivetv Auto Update"
TIMEOUT = 10
WORKERS = 10
DEFAULT_UA = "Mozilla/5.0 (BDlivetv Auto Update)"


def _store_header(headers, key, value):
    """Map a header name (any case / Kodi style) to a canonical HTTP header."""
    k = key.strip().lower().replace("_", "-")
    v = value.strip()
    if not v:
        return
    if k in ("referer", "referrer", "http-referrer", "http-referer"):
        headers["Referer"] = v
    elif k in ("user-agent", "http-user-agent"):
        headers["User-Agent"] = v
    elif k not in ("",):
        headers[key.strip()] = v


def _parse_kodiprop(line, headers):
    # Forms:
    #   #KODIPROP:inputstream.adaptive.stream_headers=Referer=..&User-Agent=..
    #   #KODIPROP:inputstream.adaptive.stream_headers="Referer=.."
    body = line.split(":", 1)[1] if ":" in line else ""
    if "=" not in body:
        return
    key, value = body.split("=", 1)
    key = key.strip().lower()
    value = value.strip().strip('"')
    if key.endswith("stream_headers"):
        for pair in value.split("&"):
            if "=" in pair:
                hk, hv = pair.split("=", 1)
                _store_header(headers, urllib.parse.unquote(hk),
                              urllib.parse.unquote(hv))
    elif "referer" in key or "referrer" in key or "user-agent" in key \
            or "user_agent" in key:
        _store_header(headers, key, value)


def _parse_vlcopt(line, headers):
    # Forms: #EXTVLCOPT:http-referrer=.. / #EXTVLCOPT:http-user-agent=..
    body = line.split(":", 1)[1] if ":" in line else ""
    if "=" not in body:
        return
    key, value = body.split("=", 1)
    _store_header(headers, key, value)


def parse_entries(text):
    """Return a list of (name, url, headers) for one M3U playlist text."""
    entries = []
    name = ""
    headers = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            headers = {}
        elif line.startswith("#KODIPROP"):
            _parse_kodiprop(line, headers)
        elif line.startswith("#EXTVLCOPT"):
            _parse_vlcopt(line, headers)
        elif line.startswith("#"):
            continue
        elif line.lower().startswith(("http://", "https://")):
            entries.append((name, line, dict(headers)))
            name = ""
            headers = {}
    return entries


SAMPLE_BYTES = 1024  # how much of a response body the probe inspects

MEDIA_CONTENT_TYPES = frozenset({
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "application/dash+xml",
})


def _fetch(url, headers, use_range):
    """GET a URL, following redirects to the final response.

    Returns (code, content_type, sample, final_url): sample is the first
    SAMPLE_BYTES of the body (gzip-decoded when needed), final_url is the
    URL after any redirects.
    """
    req_headers = {
        "User-Agent": headers.get("User-Agent", DEFAULT_UA),
        "Accept": "*/*",
    }
    for k, v in headers.items():
        if k.lower() != "user-agent":
            req_headers[k] = v
    if use_range:
        req_headers["Range"] = "bytes=0-%d" % (SAMPLE_BYTES - 1)
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        try:
            sample = resp.read(SAMPLE_BYTES)
        except Exception:
            sample = b""
        if sample[:2] == b"\x1f\x8b":  # gzip-encoded body
            try:
                sample = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(
                    sample, SAMPLE_BYTES)
            except Exception:
                pass
        content_type = resp.headers.get_content_type() if resp.headers else ""
        return resp.getcode() or 200, content_type, sample, resp.geturl()


def _looks_like_html(content_type, sample):
    if content_type == "text/html":
        return True
    head = sample.lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _is_mpeg_ts(sample):
    """MPEG-TS: 0x47 sync byte at the start of every 188-byte packet."""
    if len(sample) < 2 * 188 + 1:
        return sample[:1] == b"\x47"
    return sample[0] == 0x47 and sample[188] == 0x47 and sample[376] == 0x47


def _is_stream_like(content_type, sample):
    """True when the response looks like an actual stream, not a web page."""
    if sample.startswith(b"#EXTM3U"):
        return True
    if content_type in MEDIA_CONTENT_TYPES:
        return True
    if content_type.startswith(("video/", "audio/")):
        return True
    if content_type in ("application/octet-stream", "text/plain", "") \
            and _is_mpeg_ts(sample):
        return True
    # DASH manifest served under a generic XML content type.
    if sample.lstrip().startswith(b"<?xml") and b"<MPD" in sample:
        return True
    return False


def _classify_status_code(code):
    """Classification when only the HTTP status is known (error responses)."""
    if code == 405:
        return "ok", "HTTP %d" % code
    if code in (401, 403):
        return "restricted", "HTTP %d (host answered, access restricted)" % code
    if code >= 500:
        # A server error says nothing reliable about the stream itself;
        # from the checker's location it simply could not be verified.
        return "unknown", "HTTP %d (server error, could not verify)" % code
    return "dead", "HTTP %d" % code


def probe(url, headers):
    """Classify one stream URL. Returns (status, reason).

    status: "ok" | "restricted" | "dead" | "unknown"
    - ok:         the final response (after redirects) is stream-like —
                  an #EXTM3U playlist body, a media content type, or an
                  MPEG-TS body — or 405 (host answered)
    - restricted: 403 or 401 (host answered, access restricted)
    - dead:       404/410 (also after a plain-GET retry), or a 2xx
                  response that is not a stream (e.g. the HTML page a
                  dead shortlink redirects to)
    - unknown:    the checker could not reach a verdict from its own
                  network — timeout, DNS failure, connection/SSL error
                  or a 5xx. Unknown is NEVER reported as dead: a slow or
                  far-away host may be perfectly live for its viewers.
    """
    try:
        code, content_type, sample, _final_url = _fetch(
            url, headers, use_range=True)
    except urllib.error.HTTPError as e:
        if e.code in (416, 404):
            # Range not satisfiable -> retry once with a plain GET.
            try:
                code, content_type, sample, _final_url = _fetch(
                    url, headers, use_range=False)
            except urllib.error.HTTPError as e2:
                return _classify_status_code(e2.code)
            except Exception as e2:  # noqa: BLE001
                return "unknown", "HTTP %d then %s: %s (could not verify)" % (
                    e.code, type(e2).__name__, e2)
        else:
            return _classify_status_code(e.code)
    except (socket.timeout, TimeoutError):
        # A timeout from the checker's network (e.g. a slow host on
        # another continent) does NOT prove the stream is dead for
        # viewers near it — report it as unverified, never as dead.
        return "unknown", "timeout after %ss (could not verify)" % TIMEOUT
    except urllib.error.URLError as e:
        return "unknown", "connection error: %s (could not verify)" % (
            e.reason,)
    except Exception as e:  # noqa: BLE001
        return "unknown", "%s: %s (could not verify)" % (
            type(e).__name__, e)

    if 200 <= code < 400:
        if _looks_like_html(content_type, sample):
            return "dead", "not a stream (web page)"
        if _is_stream_like(content_type, sample):
            return "ok", "HTTP %d" % code
        return "dead", "not a stream (content-type %s)" % (
            content_type or "unknown")
    return _classify_status_code(code)


def probe_with_retry(url, headers):
    """Probe once; an "unknown" verdict gets one retry after a pause —
    transient slowness must not mislabel a live stream."""
    status, reason = probe(url, headers)
    if status == "unknown":
        time.sleep(2)
        status, reason = probe(url, headers)
    return status, reason


def main():
    app_dir = REPO_ROOT / "app"
    playlist_files = sorted(app_dir.glob("*.m3u"), key=lambda p: p.name.lower())
    print("%s: found %d playlist(s) in app/" % (GENERATOR, len(playlist_files)))

    parsed = {}       # rel path -> [(name, url, headers)]
    unique = {}       # url -> headers (first seen)
    for path in playlist_files:
        rel = "app/" + path.name
        text = path.read_text(encoding="utf-8", errors="replace")
        entries = parse_entries(text)
        parsed[rel] = entries
        for _name, url, headers in entries:
            unique.setdefault(url, headers)
        print("  %s: %d entries" % (rel, len(entries)))

    print("Probing %d unique stream URL(s)..." % len(unique))
    results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(probe_with_retry, url, hdrs): url
                   for url, hdrs in unique.items()}
        done = 0
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
            done += 1
            if done % 200 == 0:
                print("  probed %d/%d" % (done, len(unique)))

    sources = {}
    for rel in sorted(parsed):
        entries = parsed[rel]
        ok = restricted = 0
        dead = []
        unknown = []
        for name, url, _headers in entries:
            status, reason = results[url]
            if status == "ok":
                ok += 1
            elif status == "restricted":
                restricted += 1
            elif status == "unknown":
                unknown.append({"name": name, "url": url, "reason": reason})
            else:
                dead.append({"name": name, "url": url, "reason": reason})
        sources[rel] = {
            "total": len(entries),
            "ok": ok,
            "restricted": restricted,
            "dead": dead,
            "unknown": unknown,
        }
        print("  %s -> ok=%d restricted=%d dead=%d unknown=%d"
              % (rel, ok, restricted, len(dead), len(unknown)))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {
        "updatedAt": now,
        "generator": GENERATOR,
        "sources": sources,
    }
    STATUS_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print("Wrote %s" % STATUS_PATH)

    # Playlist manifest the app syncs from: every discovered app/*.m3u
    # except the built-in default (channels.m3u), sorted by display name.
    manifest_entries = []
    for path in playlist_files:
        if path.name == BUILTIN_DEFAULT:
            continue
        manifest_entries.append({
            "name": path.stem.replace("_", " "),
            "file": "app/" + path.name,
            "url": RAW_BASE + path.name,
            "tier": "free",
        })
    manifest_entries.sort(key=lambda e: e["name"].lower())
    manifest = {"updatedAt": now, "playlists": manifest_entries}
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print("Wrote %s (%d playlist(s))" % (MANIFEST_PATH, len(manifest_entries)))

    # Category + Logo + Events bots: keep app/channel_categories.json,
    # app/channel_logos.json and app/events.json in step with the
    # playlists. Best-effort - a bot failure must never break the
    # status report.
    for bot_name in ("categorize_channels.py", "fix_logos.py",
                     "fetch_events.py"):
        bot_script = REPO_ROOT / "scripts" / bot_name
        if bot_script.exists():
            try:
                subprocess.run(
                    [sys.executable, str(bot_script)],
                    check=False, timeout=3000)
            except Exception as e:  # noqa: BLE001
                print("%s failed: %s" % (bot_name, e))


if __name__ == "__main__":
    main()
