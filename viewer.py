#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "beautifulsoup4",
#     "click",
#     "pywebview",
#     "requests",
# ]
# ///

import hashlib
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from io import BytesIO
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import click
import requests
from bs4 import BeautifulSoup

os.environ['WEBKIT_DISABLE_DMABUF_RENDERER'] = '1'  # must be set before importing webview
import webview

CSS_URL_RE = re.compile(r"url\(([^)]+)\)")

@dataclass
class SnapshotResult:
    changed: bool
    html_path: Path

class SilentHTTPRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def _requests_session(user_agent: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent})
    s.timeout = 20
    return s


def safe_path_for_url(url: str, cache_root: Path) -> Path:
    """Map a URL to a safe local path under cache_root."""
    parsed = urlparse(url)
    # Build a relative path using netloc + path
    rel = Path(parsed.netloc) / parsed.path.lstrip("/")
    rel = Path(*rel.parts[1:])
    if rel.name == "":
        rel = rel / "index.html"
    # Ensure extension for routes without one
    if not rel.suffix:
        rel = rel.with_suffix(".html")
    local_path = cache_root / rel
    local_path.parent.mkdir(parents=True, exist_ok=True)
    return local_path


def extract_asset_urls(base_url: str, html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: set[str] = set()

    # <link href>, <script src>, <img src>
    for tag, attr in (("link", "href"), ("script", "src"), ("img", "src")):
        for el in soup.find_all(tag):
            u = el.get(attr)
            if not u:
                continue
            abs_u = urljoin(base_url, u)
            urls.add(abs_u)

    # CSS @import and url(...) inside inline <style>
    for style in soup.find_all("style"):
        if not style.string:
            continue
        urls.update(extract_urls_from_css(base_url, style.string))

    return urls


def extract_urls_from_css(base_url: str, css_text: str) -> set[str]:
    found: set[str] = set()
    for match in CSS_URL_RE.finditer(css_text):
        raw = match.group(1).strip().strip("\"'")
        if raw.startswith("data:"):
            continue
        found.add(urljoin(base_url, raw))
    # Basic @import capture
    for imp in re.findall(r"@import\s+(?:url\()?['\"]([^'\"]+)['\"]\)?", css_text):
        if imp.startswith("data:"):
            continue
        found.add(urljoin(base_url, imp))
    return found


def rewrite_html_to_local(html: str, base_url: str, cache_root: Path) -> str:
    """Rewrite asset URLs in HTML to point to our local cache server paths.
    We keep them as relative paths from the cache root HTTP server.
    """
    soup = BeautifulSoup(html, "html.parser")

    def to_rel(p: Path) -> str:
        # We serve from cache_root as docroot; convert local path to URL path
        rel = p.relative_to(cache_root).as_posix()
        return f"/{rel}"

    for tag, attr in (("link", "href"), ("script", "src"), ("img", "src")):
        for el in soup.find_all(tag):
            u = el.get(attr)
            if not u:
                continue
            abs_u = urljoin(base_url, u)
            local_p = safe_path_for_url(abs_u, cache_root)
            el[attr] = to_rel(local_p)

    # Optionally, set a <base> to keep relative links within cache
    if not soup.find("base"):
        base = soup.new_tag("base", href="/")
        # Prefer to insert in <head>
        head = soup.find("head")
        if head:
            head.insert(0, base)
        else:
            soup.insert(0, base)

    return str(soup)


def download_file(session: requests.Session, url: str, dest: Path) -> bool:
    """Download URL to dest. Returns True if content changed or new, else False."""
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        content = resp.content
    except Exception:
        return False

    old = dest.read_bytes() if dest.exists() else None
    if old == content:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(content)
    return True


def compute_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_snapshot(cache_root: Path, target_url: str, user_agent: str, extra_paths: Iterable[str]) -> SnapshotResult:
    """Fetch target_url and assets into cache_root. Returns whether it changed."""
    cache_root.mkdir(parents=True, exist_ok=True)
    session = _requests_session(user_agent)

    print(f"refreshing snapshot from {target_url} to {cache_root}")
    index_path = cache_root / "index.html"
    old_html = index_path.read_bytes() if index_path.exists() else None

    # Fetch main HTML
    resp = session.get(target_url)
    resp.raise_for_status()
    resp.encoding = 'utf-8';
    html_bytes = resp.content
    html_text = resp.text

    # Find assets
    assets = extract_asset_urls(target_url, html_text)
    for rel in extra_paths:
        assets.add(urljoin(target_url, rel))

    # Also fetch assets referenced by downloaded CSS files
    # We do two passes: download CSS, then parse their contents for url(...)
    css_assets: list[str] = []

    changed_any = False

    # First, download non-HTML assets (initial pass)
    for asset_url in sorted(assets):
        if asset_url.endswith(".html"):
            continue
        local = safe_path_for_url(asset_url, cache_root)
        if download_file(session, asset_url, local):
            changed_any = True
        if local.suffix.lower() in {".css"} and local.exists():
            try:
                css_text = local.read_text(encoding="utf-8", errors="ignore")
                css_assets.extend(list(extract_urls_from_css(asset_url, css_text)))
            except Exception:
                pass

    # # Second, download assets discovered in CSS
    for asset_url in sorted(set(css_assets)):
        local = safe_path_for_url(asset_url, cache_root)
        if download_file(session, asset_url, local):
            changed_any = True

    # Rewrite main HTML to local paths and store as index.html in root
    rewritten = rewrite_html_to_local(html_text, target_url, cache_root)
    new_html = rewritten.encode("utf-8")
    if old_html != new_html:
        index_path.write_bytes(new_html)
        changed_any = True

    return SnapshotResult(changed=changed_any, html_path=index_path)


# ---------------------------- THREADS --------------------------------
class RefetchThread(threading.Thread):
    def __init__(self, cache_root: Path, target_url: str, interval: int, preload_paths: list[Path], user_agent: str, reload_q: queue.Queue):
        super().__init__(daemon=True)
        self.cache_root = cache_root
        self.target_url = target_url
        self.interval = max(10, int(interval))
        self.preload_paths = preload_paths
        self.user_agent = user_agent
        self.reload_q = reload_q
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                res = fetch_snapshot(self.cache_root, self.target_url, self.user_agent, self.preload_paths)
                if res.changed:
                    self.reload_q.put("reload")
            except Exception:
                # Likely offline; just ignore
                pass
            # Sleep in small chunks to allow responsive shutdown
            slept = 0
            while slept < self.interval and not self._stop.is_set():
                time.sleep(1)
                slept += 1


class ReloadWatcher(threading.Thread):
    def __init__(self, window: webview.Window, reload_q: queue.Queue):
        super().__init__(daemon=True)
        self.window = window
        self.reload_q = reload_q
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                msg = self.reload_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if msg == "reload":
                try:
                    self.window.evaluate_js("location.reload(true)")
                    # self.window.load_url(self.window.url)
                except Exception:
                    pass


# --------------------------- APP ENTRY --------------------------------

def start_http_server(doc_root: Path, port: int) -> ThreadingHTTPServer:
    os.chdir(doc_root)
    server = ThreadingHTTPServer(("127.0.0.1", port), SilentHTTPRequestHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def ensure_initial_cache(cfg: any) -> Path:
    cache_root = Path(cfg["cache-dir"]).resolve()
    target_url = cfg["target-url"]

    try:
        res = fetch_snapshot(cache_root, target_url, cfg["user-agent"], cfg["preload-paths"])
        return res.html_path
    except Exception:
        # Offline: create a minimal placeholder if nothing exists
        index = cache_root / "index.html"
        cache_root.mkdir(parents=True, exist_ok=True)
        backup_index = "./assets/backup.html"
        backup_css = "./assets/backup.css"
        shutil.copy(backup_index, index) if Path(backup_index).exists() else None
        shutil.copy(backup_css, cache_root / "backup.css") if Path(backup_css).exists() else None
        if not index.exists():
            index.write_text(
                f"""
                <!doctype html>
                <meta charset=\"utf-8\">
                <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>
                <title>Offline</title>
                <style>body{{font-family:system-ui,Arial;margin:2rem;}} .badge{{display:inline-block;padding:.25rem .5rem;border:1px solid #999;border-radius:.5rem;}}
                .muted{{color:#555}}</style>
                <h1>Offline snapshot not yet available</h1>
                <p class=\"muted\">I'll keep trying to fetch <code>{target_url}</code> whenever there's a connection.</p>
                <p><span class=\"badge\">Status</span> waiting for first successful sync…</p>
                """,
                encoding="utf-8",
            )
        return index


@click.command()
@click.option("--config", "-c", type=str, default="./config.json", help="Path to config file")
def main(config: str | Path):
    config = Path(config)
    if not config.exists():
        print(f"Config file {config} not found. Aborting.")
        sys.exit(1)

    cfg = dict()
    with open(config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    target_url = cfg["target-url"]
    cache_root: Path = Path(cfg["cache-dir"])
    port = cfg["port"]

    abs_cache_root = cache_root.resolve()

    index_path = ensure_initial_cache(cfg)

    server = start_http_server(abs_cache_root, port)
    start_url = f"http://127.0.0.1:{port}/{index_path.name}"
    window = webview.create_window("Offline Mirror", url=start_url)

    reload_q: queue.Queue = queue.Queue()
    refetcher = RefetchThread(
        abs_cache_root,
        target_url,
        cfg["refresh-interval-sec"],
        [Path(p) for p in cfg["preload-paths"]],
        cfg["user-agent"],
        reload_q,
    )
    watcher = ReloadWatcher(window, reload_q)

    refetcher.start()
    watcher.start()

    try:
        webview.start()
    finally:
        refetcher.stop()
        watcher.stop()
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
