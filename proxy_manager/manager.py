#!/usr/bin/env python3
"""Orchestration CLI: create GoLogin profiles, then launch them.

Usage:
    python run.py --mode init   # one-time; creates profiles if missing
    python run.py --mode start  # boots each profile sequentially so you can work
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from typing import Any, Dict

import yaml
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from tenacity import retry, wait_fixed, stop_after_attempt

from proxy_manager.api.gologin import GoLogin

load_dotenv()

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE_FILE = PROJECT_ROOT / ".profiles.json"
CONFIG_DIR = PROJECT_ROOT / "proxy_manager" / "config"

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def load_cache() -> Dict[str, str]:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}

def save_cache(data: Dict[str, str]) -> None:
    CACHE_FILE.write_text(json.dumps(data, indent=2))


def proxy_dict(server: str, username: str, password: str) -> dict[str, Any]:
    host, port = server.split(":")
    return {
        "mode": "http",
        "host": host,
        "port": int(port),
        "username": username,
        "password": password,
    }


@retry(wait=wait_fixed(3), stop=stop_after_attempt(4))
async def open_window(playwright, ws_url: str, acc_id: str):
    browser = await playwright.chromium.connect_over_cdp(ws_url)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await context.new_page()

    # quick check we’re on the correct IP
    await page.goto("https://ip.oxylabs.io/location", wait_until="domcontentloaded")
    await page.goto("https://www.linkedin.com/", wait_until="domcontentloaded")

    print(f"[{acc_id}] Window ready ➜ create / login account, then press ENTER in terminal…")
    input()

    storage_dir = PROJECT_ROOT / "profiles"
    storage_dir.mkdir(exist_ok=True)
    out_file = storage_dir / f"{acc_id}_storage.json"
    await context.storage_state(path=str(out_file))
    print(f"[{acc_id}] Storage snapshot saved to {out_file}")

    await browser.close()


async def cli(mode: str, *, only_ids: set[str] | None = None):
    # read accounts config
    cfg_path = CONFIG_DIR / "accounts.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    accounts: list[dict] = cfg["accounts"]

    if only_ids:
        accounts = [acc for acc in accounts if acc["id"] in only_ids]
        if not accounts:
            print(f"No accounts match --only {', '.join(only_ids)}", file=sys.stderr)
            return

    # GoLogin client
    gl = GoLogin()

    cache = load_cache()

    if mode == "init":
        updated = False
        for acc in accounts:
            acc_id = acc["id"]
            if acc_id in cache:
                print(f"{acc_id}: already exists (profile id={cache[acc_id]})")
                continue

            usr = os.environ[acc["oxy_user_env"]]
            pwd = os.environ[acc["oxy_pass_env"]]
            proxy_server = f"isp.oxylabs.io:{acc['proxy_port']}"

            profile_id = gl.create_profile(
                name=acc_id,
                region=acc["region"],
                timezone=acc["timezone"],
                locale=acc["locale"],
                proxy_conf=proxy_dict(proxy_server, usr, pwd),
            )
            print(f"{acc_id}: created GoLogin profile {profile_id}")
            cache[acc_id] = profile_id
            updated = True
        if updated:
            save_cache(cache)
        print("Init done.")

    elif mode == "start":
        async with async_playwright() as pw:
            for acc in accounts:
                acc_id = acc["id"]
                profile_id = cache.get(acc_id)
                if not profile_id:
                    print(f"{acc_id}: missing profile – run init first", file=sys.stderr)
                    continue
                ws_url = gl.start_profile(profile_id)
                print(f"{acc_id}: profile started")
                await open_window(pw, ws_url, acc_id)
                gl.stop_profile(profile_id)
                print(f"{acc_id}: stopped\n")
    elif mode == "local":
        # Launch local persistent contexts with proxies for manual interaction
        async with async_playwright() as pw:
            for acc in accounts:
                acc_id = acc["id"]
                acc_tag = tag(acc)
                usr = os.getenv(acc["oxy_user_env"])
                pwd = os.getenv(acc["oxy_pass_env"])
                if not usr or not pwd:
                    print(f"{acc_id}: missing proxy creds; skip", file=sys.stderr)
                    continue
                proxy_server = f"http://isp.oxylabs.io:{acc['proxy_port']}"
                user_dir = PROJECT_ROOT / "profiles" / acc_id
                user_dir.mkdir(parents=True, exist_ok=True)
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(user_dir),
                    channel="chrome",
                    headless=False,
                    proxy={"server": proxy_server, "username": usr, "password": pwd},
                    # Remove automation flag AND allow Chrome Web Store installs
                    ignore_default_args=["--enable-automation", "--disable-extensions"],
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                        "--webrtc-stun-probe-timeout=2000",
                        "--no-default-browser-check",
                        "--no-first-run",
                        f"--lang={acc.get('locale','en-US')}",
                    ],
                    locale=acc.get("locale", "en-US"),
                    timezone_id=acc.get("timezone", "UTC"),
                    geolocation={
                        "latitude": acc["geo"]["lat"],
                        "longitude": acc["geo"]["lon"],
                    },
                    permissions=["geolocation"],
                )
                # Inject small stealth script to mask webdriver and related properties
                await ctx.add_init_script(
                    """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
                    Object.defineProperty(navigator, 'platform',  { get: () => 'Win32' });
                    try { window.chrome = window.chrome || { runtime: {} }; } catch(e) {}
                    """
                )
                page = await ctx.new_page()
                await page.goto("https://ip.oxylabs.io/location")
                print(f"[{acc_tag}] Local window opened. Complete actions then press ENTER here…")
                input()
                # If there is a CRX extension in project root, auto-load it
                ext_crx = PROJECT_ROOT / "HeyReach.crx"
                if ext_crx.exists():
                    await ctx.close()
                    load_args = [
                        f"--disable-extensions-except={ext_crx}",
                        f"--load-extension={ext_crx}",
                    ]
                    ctx = await pw.chromium.launch_persistent_context(
                        user_data_dir=str(user_dir),
                        headless=False,
                        channel="chrome",
                        proxy={"server": proxy_server, "username": usr, "password": pwd},
                        ignore_default_args=["--enable-automation", "--disable-extensions"],
                        args=load_args + [
                            "--no-sandbox",
                        ],
                    )
                    page = await ctx.new_page()
                await ctx.close()
        print("Local sessions finished.")

    else:
        print("Mode must be 'init', 'start', or 'local'", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["init", "start", "local"], required=True,
                        help="What action to run: init (create GoLogin profiles), start (cloud profiles via GoLogin), local (open local persistent windows).")
    parser.add_argument("--only", nargs="*", default=[],
                        help="Optional list of account IDs (e.g. li-it-001) to process. If omitted, all accounts are processed in config order.")

    args = parser.parse_args()

    # Pass filter list down
    asyncio.run(cli(args.mode, only_ids=set(args.only)))


if __name__ == "__main__":
    main()