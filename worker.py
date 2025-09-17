import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

from utils import parse_proxy

# Try patchright first, then fallback to playwright
try:
    from patchright.async_api import async_playwright
    ENGINE = "patchright"
except Exception:
    from playwright.async_api import async_playwright
    ENGINE = "playwright"

HEARTBEAT_SEC = 1.0

class GracefulExit(Exception):
    pass

def _install_signal_handlers(loop):
    def _handler(signum, frame):
        raise GracefulExit()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

async def run_worker(profile_id: str, name: str, proxy: str | None, ws_url: str, profile_dir: str):
    # Late import to keep start-up quick
    import websockets

    server, creds = parse_proxy(proxy)
    context_kwargs = {}
    if server:
        context_kwargs["proxy"] = {"server": server, **(creds or {})}

    # Ensure profile dir exists
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=[],
            **context_kwargs
        )
        page = await browser.new_page()
        try:
            await page.goto("https://www.example.com", wait_until="domcontentloaded")
        except Exception:
            pass

        async def heartbeat():
            while True:
                try:
                    url = page.url if page else ""
                except Exception:
                    url = ""
                msg = {
                    "type": "heartbeat",
                    "profile_id": profile_id,
                    "state": "running",
                    "url": url,
                    "engine": ENGINE,
                    "ts": time.time()
                }
                try:
                    async with websockets.connect(ws_url, ping_interval=None) as ws:
                        await ws.send(json.dumps(msg))
                except Exception:
                    # ignore WS errors, we'll try again next tick
                    pass
                await asyncio.sleep(HEARTBEAT_SEC)

        hb_task = asyncio.create_task(heartbeat())

        # Wait until canceled
        try:
            while True:
                await asyncio.sleep(0.2)
        except GracefulExit:
            pass
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except Exception:
                pass
            await browser.close()

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="Profile ID")
    parser.add_argument("--name", required=True, help="Profile name")
    parser.add_argument("--proxy", default=None, help="Proxy string or empty")
    parser.add_argument("--ws", required=True, help="WebSocket ws://... endpoint for heartbeats")
    parser.add_argument("--dir", required=True, help="Profile user data dir")
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(run_worker(args.id, args.name, args.proxy if args.proxy != "" else None, args.ws, args.dir))
    except GracefulExit:
        pass
    finally:
        loop.close()

if __name__ == "__main__":
    main()
