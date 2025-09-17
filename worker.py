import asyncio
import json
import os
import signal
import sys
import time
import logging
from pathlib import Path
from typing import Optional, Dict, Any

from patchright.async_api import async_playwright  # жёстко требуем Patchright
from utils import (
    parse_proxy,
    resolve_timezone_via_proxy,
    build_chromium_ua,
    sanitize_headers_for_version,
)

ENGINE = "patchright"

HEARTBEAT_SEC = 1.0
LOG = logging.getLogger("worker")
if not LOG.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

class GracefulExit(Exception):
    pass

def _install_signal_handlers(loop):
    def _handler(signum, frame):
        raise GracefulExit()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

async def run_worker(profile_id: str, name: str, proxy: str | None, ws_url: str, profile_dir: str):
    import websockets  # late import

    LOG.info("ENGINE selected: %s", ENGINE)
    try:
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        (Path(profile_dir) / "engine.txt").write_text(ENGINE, encoding="utf-8")
    except Exception:
        LOG.exception("Failed to persist engine marker")

    # Optional fingerprint helpers (не критично, если их нет)
    try:
        from utils import load_or_create_profile_fingerprint, build_init_script_from_fingerprint  # type: ignore
    except Exception:
        load_or_create_profile_fingerprint = None  # type: ignore
        build_init_script_from_fingerprint = None  # type: ignore

    # ---- Proxy -> context kwargs ----
    try:
        server, creds = parse_proxy(proxy)
    except Exception as e:
        LOG.warning("Proxy parse error (%s). Starting without proxy.", e)
        server, creds = None, None

    context_kwargs: Dict[str, Any] = {}
    if server:
        context_kwargs["proxy"] = {"server": server, **(creds or {})}

    # Ensure profile dir exists
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    # ---- Таймзона под egress-IP через сам прокси ----
    tz = None
    try:
        tz = await resolve_timezone_via_proxy(server, creds)
        if tz:
            context_kwargs["timezone_id"] = tz
            LOG.info("Resolved timezone via proxy egress: %s", tz)
    except Exception:
        LOG.exception("resolve_timezone_via_proxy failed")

    # ---- Fingerprint -> user_agent/locale/viewport/headers ----
    fingerprint: Optional[Dict[str, Any]] = None
    if load_or_create_profile_fingerprint:
        try:
            fingerprint = load_or_create_profile_fingerprint(profile_dir)
        except Exception:
            LOG.exception("Failed to load/create fingerprint")

    # Базовые значения из fingerprint перед запуском
    nav = (fingerprint or {}).get("navigator") or {}
    raw_headers = (fingerprint or {}).get("headers") or {}
    hdrs: Dict[str, str] = {str(k): str(v) for k, v in raw_headers.items()} if raw_headers else {}
    locale = nav.get("language") or hdrs.get("Accept-Language") or hdrs.get("accept-language")

    if locale:
        context_kwargs.setdefault("locale", locale)

    screen = (fingerprint or {}).get("screen") or {}
    if screen.get("width") and screen.get("height"):
        try:
            context_kwargs.setdefault("viewport", {"width": int(screen["width"]), "height": int(screen["height"])})
        except Exception:
            pass

    # ---- WebRTC leak mitigation ----
    launch_args = [
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-features=WebRtcHideLocalIpsWithMdns",
    ]

    # ---- Запуск Patchright ----
    async with async_playwright() as p:
        # Достаём версию установленного Chromium ДО старта контекста
        chromium_version_str = None
        chromium_major: Optional[int] = None
        try:
            chromium_version_str = getattr(p.chromium, "version", None)  # строка вида "119.0.6045.0"
            if chromium_version_str:
                chromium_major = int(str(chromium_version_str).split(".", 1)[0])
        except Exception:
            chromium_version_str = None
            chromium_major = None

        # Выравниваем UA под реальную версию: строим свежий UA ещё до запуска
        if chromium_major:
            ua_built = build_chromium_ua(chromium_major)
            context_kwargs["user_agent"] = ua_built
            # заголовок User-Agent через set_extra_http_headers не всегда уважается,
            # поэтому кладём его через user_agent при старте контекста.
            # Доп. заголовки подготовим ниже (без UA и без sec-ch-ua*)
            hdrs = sanitize_headers_for_version(hdrs, chromium_major)
        else:
            # если не смогли узнать версию заранее — оставим hdrs как есть (ниже всё равно сэними)
            hdrs = sanitize_headers_for_version(hdrs, -1)

        # Стартуем persistent context
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=launch_args,
            **context_kwargs
        )

        # ---- Post-launch: headers + init script ----
        try:
            # Accept-Language, если не проставили
            if nav.get("language") and not any(k.lower() == "accept-language" for k in hdrs):
                hdrs["Accept-Language"] = str(nav.get("language"))

            if hdrs:
                try:
                    await browser.set_extra_http_headers(hdrs)
                except Exception:
                    LOG.exception("set_extra_http_headers failed")

            if build_init_script_from_fingerprint and fingerprint:
                try:
                    script = build_init_script_from_fingerprint(fingerprint)
                    if script:
                        await browser.add_init_script(script)
                except Exception:
                    LOG.exception("add_init_script failed")
        except Exception:
            LOG.exception("post-launch fingerprint application failed")

        # ---- Первая вкладка ----
        page = None
        try:
            page = await browser.new_page()
            try:
                await page.goto("https://www.example.com", wait_until="domcontentloaded")
            except Exception:
                pass
        except Exception:
            LOG.exception("Failed to create initial page")

        # ---- Персистентный heartbeat (одно WS-соединение + реконнект) ----
        async def heartbeat():
            backoff = 1
            while True:
                try:
                    async with websockets.connect(ws_url, ping_interval=None) as ws:
                        backoff = 1
                        while True:
                            current_url = ""
                            try:
                                current_url = page.url if page else ""
                            except Exception:
                                current_url = ""
                            msg = {
                                "type": "heartbeat",
                                "profile_id": profile_id,
                                "state": "running",
                                "url": current_url,
                                "engine": ENGINE,
                                "ts": time.time()
                            }
                            await ws.send(json.dumps(msg))
                            await asyncio.sleep(HEARTBEAT_SEC)
                except Exception:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 15)

        hb_task = asyncio.create_task(heartbeat())

        # ---- Ожидание сигнала остановки ----
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
            try:
                await browser.close()
            except Exception:
                LOG.exception("Error closing browser")

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
        loop.run_until_complete(
            run_worker(
                args.id,
                args.name,
                args.proxy if args.proxy != "" else None,
                args.ws,
                args.dir,
            )
        )
    except GracefulExit:
        pass
    finally:
        loop.close()

if __name__ == "__main__":
    main()
