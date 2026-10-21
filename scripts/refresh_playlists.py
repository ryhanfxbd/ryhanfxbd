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
import ssl
import urllib.error
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


def _fetch(url, headers, use_range):
    req_headers = {
        "User-Agent": headers.get("User-Agent", DEFAULT_UA),
        "Accept": "*/*",
    }
    for k, v in headers.items():
        if k.lower() != "user-agent":
            req_headers[k] = v
    if use_range:
        req_headers["Range"] = "bytes=0-0"
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        try:
            resp.read(512)
        except Exception:
            pass
        return resp.getcode() or 200


def probe(url, headers):
    """Classify one stream URL. Returns (status, reason).

    status: "ok" | "restricted" | "dead"
    - ok:         2xx/3xx, or 405 (host answered)
    - restricted: 403 or 401 (host answered, access restricted)
    - dead:       connection failure, timeout, 404/410, 5xx, other errors
    """
    try:
        code = _fetch(url, headers, use_range=True)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            # Range not satisfiable -> retry once with a plain GET.
            try:
                code = _fetch(url, headers, use_range=False)
            except urllib.error.HTTPError as e2:
                code = e2.code
            except Exception as e2:  # noqa: BLE001
                return "dead", "HTTP 416 then %s: %s" % (type(e2).__name__, e2)
        else:
            code = e.code
    except (socket.timeout, TimeoutError):
        return "dead", "timeout after %ss" % TIMEOUT
    except urllib.error.URLError as e:
        return "dead", "connection error: %s" % (e.reason,)
    except Exception as e:  # noqa: BLE001
        return "dead", "%s: %s" % (type(e).__name__, e)

    if 200 <= code < 400 or code == 405:
        return "ok", "HTTP %d" % code
    if code in (401, 403):
        return "restricted", "HTTP %d (host answered, access restricted)" % code
    return "dead", "HTTP %d" % code


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
        futures = {pool.submit(probe, url, hdrs): url
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
        for name, url, _headers in entries:
            status, reason = results[url]
            if status == "ok":
                ok += 1
            elif status == "restricted":
                restricted += 1
            else:
                dead.append({"name": name, "url": url, "reason": reason})
        sources[rel] = {
            "total": len(entries),
            "ok": ok,
            "restricted": restricted,
            "dead": dead,
        }
        print("  %s -> ok=%d restricted=%d dead=%d"
              % (rel, ok, restricted, len(dead)))

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


if __name__ == "__main__":
    main()
