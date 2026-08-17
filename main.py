#!/usr/bin/env python3
"""
onion_crawler.py

Crawls a list of seed URLs (clearnet or .onion) through the Tor SOCKS proxy,
extracts .onion links found on those pages, and checks which v3 addresses
are actually reachable.

IMPORTANT:
  - v2 onion addresses (16 chars, e.g. abcdefghij123456.onion) are DEAD.
    Tor removed v2 support from the network in October 2021. This script
    still extracts v2-looking strings (for cataloging/archival purposes)
    but does NOT attempt to "check" them as live, because nothing will
    ever answer on that protocol again.
  - v3 onion addresses are 56 base32 characters before ".onion"
    (e.g. abcd...xyz1234567890abcdefghijklmnopqrstuvwxyz234567.onion)

Requirements (in your Whonix Workstation venv):
    pip install requests[socks] beautifulsoup4

Usage:
    1. Make sure Tor is running and reachable (on Whonix, the default
       SocksPort is usually 127.0.0.1:9050 inside the Workstation via
       the Whonix gateway -- verify with `curl --socks5-hostname 127.0.0.1:9050 https://check.torproject.org`).
    2. Put your seed URLs in seeds.txt, one per line.
    3. Run: python3 onion_crawler.py seeds.txt --out results.json
"""

import argparse
import json
import re
import sys
import time
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

# --- Tor SOCKS proxy config -------------------------------------------------
TOR_SOCKS_HOST = "127.0.0.1"
TOR_SOCKS_PORT = 9050  # Whonix Workstation default; adjust if you use a different port

PROXIES = {
    "http": f"socks5h://{TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}",
    "https": f"socks5h://{TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:102.0) Gecko/20100101 Firefox/102.0"
}

# --- Regexes -----------------------------------------------------------------
# v3: 56 base32 chars + .onion
V3_RE = re.compile(r"\b[a-z2-7]{56}\.onion\b", re.IGNORECASE)
# v2 (legacy/dead): 16 base32 chars + .onion
V2_RE = re.compile(r"\b[a-z2-7]{16}\.onion\b", re.IGNORECASE)

REQUEST_TIMEOUT = 25
MAX_WORKERS = 8
CRAWL_DELAY = 1.0  # seconds between requests to be gentle on the network


def fetch(url, session):
    """Fetch a URL through Tor. Returns (status_code, text) or (None, None) on failure."""
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS, allow_redirects=True)
        return resp.status_code, resp.text
    except requests.RequestException as e:
        return None, str(e)


def extract_onion_links(text, base_url=None):
    """Pull unique onion hostnames out of raw text/HTML, plus href-based links."""
    found = set()

    for m in V3_RE.findall(text):
        found.add(m.lower())
    for m in V2_RE.findall(text):
        found.add(m.lower())

    # Also parse <a href> tags in case links are relative or oddly formatted
    try:
        soup = BeautifulSoup(text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            full = urljoin(base_url, href) if base_url else href
            host = urlparse(full).netloc.lower()
            if host.endswith(".onion"):
                if V3_RE.fullmatch(host) or V2_RE.fullmatch(host):
                    found.add(host)
    except Exception:
        pass

    return found


def check_v3_alive(onion_host, session):
    """Try both http and https on a v3 onion host to see if anything answers."""
    for scheme in ("http", "https"):
        url = f"{scheme}://{onion_host}/"
        code, _ = fetch(url, session)
        if code is not None:
            return True, scheme, code
    return False, None, None


def crawl(seed_urls, max_depth=1):
    """
    Crawl seed URLs (and optionally links found on them, up to max_depth)
    through Tor, collecting all onion hostnames encountered.
    """
    session = requests.Session()
    session.proxies.update(PROXIES)

    all_onions = set()
    visited = set()
    frontier = [(u, 0) for u in seed_urls]

    while frontier:
        url, depth = frontier.pop(0)
        if url in visited:
            continue
        visited.add(url)

        print(f"[crawl] fetching {url} (depth {depth})", file=sys.stderr)
        code, text = fetch(url, session)
        time.sleep(CRAWL_DELAY)

        if code is None:
            print(f"[crawl]   failed: {text}", file=sys.stderr)
            continue

        onions = extract_onion_links(text, base_url=url)
        all_onions.update(onions)
        print(f"[crawl]   found {len(onions)} onion refs (status {code})", file=sys.stderr)

        if depth < max_depth:
            try:
                soup = BeautifulSoup(text, "html.parser")
                for a in soup.find_all("a", href=True):
                    link = urljoin(url, a["href"])
                    if link not in visited:
                        frontier.append((link, depth + 1))
            except Exception:
                pass

    return all_onions


def verify(onion_hosts):
    """Check liveness of v3 onion hosts concurrently. v2 hosts are reported as dead-by-design."""
    results = {"v3_alive": [], "v3_dead": [], "v2_unsupported": []}

    v3_hosts = [h for h in onion_hosts if V3_RE.fullmatch(h)]
    v2_hosts = [h for h in onion_hosts if V2_RE.fullmatch(h)]
    results["v2_unsupported"] = sorted(v2_hosts)

    session_factory = lambda: (lambda s: (s.proxies.update(PROXIES), s)[1])(requests.Session())

    def worker(host):
        sess = session_factory()
        alive, scheme, code = check_v3_alive(host, sess)
        return host, alive, scheme, code

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker, h): h for h in v3_hosts}
        for fut in as_completed(futures):
            host, alive, scheme, code = fut.result()
            if alive:
                print(f"[check] ALIVE  {host}  ({scheme}, {code})", file=sys.stderr)
                results["v3_alive"].append({"host": host, "scheme": scheme, "status": code})
            else:
                print(f"[check] dead   {host}", file=sys.stderr)
                results["v3_dead"].append(host)

    return results


def main():
    parser = argparse.ArgumentParser(description="Crawl seed URLs via Tor and extract/verify onion links.")
    parser.add_argument("seeds", help="Path to a text file with one seed URL per line")
    parser.add_argument("--out", default="results.json", help="Output JSON file")
    parser.add_argument("--depth", type=int, default=1, help="Crawl depth from each seed (default 1)")
    args = parser.parse_args()

    with open(args.seeds) as f:
        seeds = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    if not seeds:
        print("No seed URLs found in input file.", file=sys.stderr)
        sys.exit(1)

    # Sanity check: confirm Tor proxy is actually reachable before crawling
    try:
        test_sess = requests.Session()
        test_sess.proxies.update(PROXIES)
        r = test_sess.get("https://check.torproject.org/", timeout=REQUEST_TIMEOUT, headers=HEADERS)
        if "Congratulations" not in r.text:
            print("[warn] Tor check page didn't confirm Tor usage -- verify your proxy settings.", file=sys.stderr)
        else:
            print("[ok] Confirmed traffic is routed through Tor.", file=sys.stderr)
    except requests.RequestException as e:
        print(f"[error] Could not reach Tor SOCKS proxy at {TOR_SOCKS_HOST}:{TOR_SOCKS_PORT}: {e}", file=sys.stderr)
        print("Check that Tor is running and the SocksPort is correct for your Whonix setup.", file=sys.stderr)
        sys.exit(1)

    onions = crawl(seeds, max_depth=args.depth)
    print(f"[crawl] total unique onion hostnames found: {len(onions)}", file=sys.stderr)

    results = verify(onions)

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nDone. {len(results['v3_alive'])} live v3 onions, "
          f"{len(results['v3_dead'])} unreachable v3 onions, "
          f"{len(results['v2_unsupported'])} v2 strings found (unsupported by Tor since 2021).")
    print(f"Full results written to {args.out}")


if __name__ == "__main__":
    main()
