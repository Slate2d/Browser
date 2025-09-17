# utils.py
# Утилиты: парсинг прокси + BrowserForge-backed fingerprint generation & helpers

import re
import json
import random
import logging
from typing import Optional, Tuple, Dict, Any
from pathlib import Path
# utils.py (добавь в конец файла)
import asyncio
from typing import Optional
def build_chromium_ua(major: int, platform: str = "Windows NT 10.0; Win64; x64") -> str:
    # максимально типовой, без фантазии
    return (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )

def sanitize_headers_for_version(headers: dict, chromium_major: int) -> dict:
    """
    Убираем/правим заголовки, которые палятся версией.
    Если sec-ch-ua не совпадает по major — просто выкидываем его (пусть Chromium сам проставит).
    """
    out = {k: v for k, v in (headers or {}).items()}
    for key in list(out.keys()):
        lk = key.lower()
        if lk in ("sec-ch-ua", "sec-ch-ua-full-version", "sec-ch-ua-full-version-list"):
            # проще убрать, чем попадать пальцем в сложный формат
            out.pop(key, None)
        if lk == "user-agent":
            # UA поставим отдельно, синхронизировав с реальным движком
            out.pop(key, None)
    return out

def _build_requests_proxy_url(server: str, creds: Optional[dict] = None) -> str:
    # server уже в виде scheme://host:port
    if not creds or not creds.get("username"):
        return server
    from urllib.parse import quote
    u = quote(str(creds["username"]), safe="")
    p = quote(str(creds.get("password","")), safe="")
    scheme, rest = server.split("://", 1)
    return f"{scheme}://{u}:{p}@{rest}"

async def resolve_timezone_via_proxy(server: Optional[str], creds: Optional[dict]) -> Optional[str]:
    """
    Возвращает tz вроде 'Europe/Warsaw', делая HTTP-запрос ЧЕРЕЗ ПРОКСИ.
    Использую два публичных сервиса как fallback: ipapi.co и ipwho.is.
    Если оба недоступны — верну None.
    """
    try:
        import httpx
    except Exception:
        return None

    if not server:
        return None

    proxy_url = _build_requests_proxy_url(server, creds)
    timeout = httpx.Timeout(6.0, connect=6.0)
    async with httpx.AsyncClient(proxies=proxy_url, timeout=timeout, verify=False) as client:
        # 1) ipapi
        try:
            r = await client.get("https://ipapi.co/json")
            if r.status_code == 200:
                tz = (r.json() or {}).get("timezone")
                if tz and isinstance(tz, str):
                    return tz
        except Exception:
            pass
        # 2) ipwho.is
        try:
            r = await client.get("https://ipwho.is/")
            if r.status_code == 200:
                tz = ((r.json() or {}).get("timezone") or {}).get("id")
                if tz and isinstance(tz, str):
                    return tz
        except Exception:
            pass
    return None

# -------------------
# Proxy parsing (взято из старой версии)
# -------------------
def parse_proxy(proxy: str | None) -> tuple[Optional[str], Optional[dict]]:
    """
    Return (server, credentials_dict_or_None) for Playwright.
    Accepts:
      - scheme://host:port
      - scheme://user:pass@host:port
    """
    if not proxy:
        return None, None
    # Basic validation
    m = re.match(
        r'^(?P<scheme>\w+)://(?:(?P<user>[^:@]+):(?P<pwd>[^@]+)@)?(?P<host>[^:]+):(?P<port>\d+)$',
        proxy.strip()
    )
    if not m:
        raise ValueError("Invalid proxy format. Expected scheme://host:port or scheme://user:pass@host:port")
    d = m.groupdict()
    scheme = d["scheme"]
    host = d["host"]
    port = int(d["port"])
    user = d.get("user")
    pwd = d.get("pwd")
    server = f"{scheme}://{host}:{port}"
    creds = None
    if user:
        creds = {"username": user, "password": pwd}
    return server, creds


# -------------------
# BrowserForge-backed fingerprint & UA utilities
# -------------------
_GENERATED_FP_FILENAME = "generated_fingerprint.json"


def _simple_fallback_ua() -> str:
    """
    Простая удачная заглушка для User-Agent, если browserforge недоступен.
    """
    browsers = [
        ("Chrome", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36"),
        ("Firefox", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{ver}.0) Gecko/20100101 Firefox/{ver}.0"),
        ("Edge", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver}.0.0.0 Safari/537.36 Edg/{ver}.0.0.0"),
        ("Safari", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_{minor}) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{ver}.1 Safari/605.1.15"),
    ]
    bname, template = random.choice(browsers)
    if bname == "Safari":
        ver = random.randint(13, 16)
        minor = random.randint(0, 7)
    else:
        ver = random.randint(100, 140)
        minor = 0
    return template.format(ver=ver, minor=minor)


def generate_fingerprint_with_browserforge() -> Optional[Dict[str, Any]]:
    """
    Try to use browserforge to generate a detailed fingerprint.
    Returns a dict or None on failure.
    The returned dict may contain:
      - user_agent (str)
      - headers (dict)
      - navigator (dict)
      - screen (dict)
    """
    try:
        # pip install browserforge[all]
        from browserforge.fingerprints import FingerprintGenerator
    except Exception:
        logging.getLogger(__name__).debug("browserforge not installed or import failed")
        return None

    try:
        gen = FingerprintGenerator()
        fp = gen.generate()
    except Exception as e:
        logging.getLogger(__name__).warning("browserforge generation failed: %s", e)
        return None

    out: Dict[str, Any] = {}
    try:
        # navigator-like properties
        nav: Dict[str, Any] = {}
        try:
            nav_obj = getattr(fp, "navigator", fp)
            for key in ("userAgent", "platform", "language", "languages",
                        "hardwareConcurrency", "deviceMemory", "vendor"):
                try:
                    val = getattr(nav_obj, key)
                except Exception:
                    try:
                        val = nav_obj.get(key)
                    except Exception:
                        val = None
                if val is not None:
                    nav[key] = val
        except Exception:
            pass
        out["navigator"] = nav

        # headers
        headers: Dict[str, Any] = {}
        try:
            hdrs = getattr(fp, "headers", None)
            if hdrs:
                headers.update({k: v for k, v in dict(hdrs).items()})
        except Exception:
            pass
        out["headers"] = headers

        # user agent
        ua = nav.get("userAgent") or headers.get("User-Agent") or headers.get("user-agent")
        if ua:
            out["user_agent"] = str(ua)

        # sec-ch-ua
        try:
            sc = headers.get("sec-ch-ua") or headers.get("Sec-CH-UA")
            if sc:
                out["sec_ch_ua"] = sc
        except Exception:
            pass

        # screen info
        screen: Dict[str, Any] = {}
        try:
            screen_obj = getattr(fp, "screen", None)
            if screen_obj:
                for k in ("width", "height", "colorDepth", "pixelDepth"):
                    try:
                        screen[k] = getattr(screen_obj, k)
                    except Exception:
                        try:
                            screen[k] = screen_obj.get(k)
                        except Exception:
                            pass
        except Exception:
            pass
        out["screen"] = screen

        # Accept-Language fallback
        if not headers.get("Accept-Language"):
            if nav.get("language"):
                headers["Accept-Language"] = nav["language"]

        out["headers"] = headers

        return out
    except Exception as e:
        logging.getLogger(__name__).warning("error parsing browserforge fingerprint: %s", e)
        return out or None


def load_or_create_profile_fingerprint(profile_dir: str, *, force_regen: bool = False) -> Dict[str, Any]:
    """
    Loads or generates a fingerprint for a profile and persists it.
    Saved to: <profile_dir>/generated_fingerprint.json
    """
    p = Path(profile_dir)
    p.mkdir(parents=True, exist_ok=True)
    saved = p / _GENERATED_FP_FILENAME

    if saved.exists() and not force_regen:
        try:
            data = json.loads(saved.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("user_agent"):
                return data
        except Exception:
            logging.getLogger(__name__).debug("existing fingerprint unreadable; regenerating")

    fp = generate_fingerprint_with_browserforge()
    if not fp:
        ua = _simple_fallback_ua()
        fp = {
            "user_agent": ua,
            "headers": {"User-Agent": ua},
            "navigator": {"userAgent": ua},
            "screen": {"width": 1920, "height": 1080}
        }

    try:
        saved.write_text(json.dumps(fp, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logging.getLogger(__name__).warning("Could not save generated fingerprint to %s", saved)

    return fp


def build_init_script_from_fingerprint(fp: Dict[str, Any]) -> str:
    """
    Build JS snippet to inject via context.add_init_script to set navigator/screen properties.
    Best-effort; browsers harden certain properties and not all overrides will succeed.
    """
    nav = fp.get("navigator", {}) if isinstance(fp, dict) else {}
    screen = fp.get("screen", {}) if isinstance(fp, dict) else {}
    headers = fp.get("headers", {}) if isinstance(fp, dict) else {}

    def to_js(v):
        return json.dumps(v, ensure_ascii=False)

    parts = []

    # navigator overrides
    nav_overrides = {}
    for k in ("userAgent", "platform", "language", "languages", "hardwareConcurrency", "deviceMemory", "vendor"):
        if k in nav:
            nav_overrides[k] = nav[k]
    if nav_overrides:
        parts.append(
            '(function(){try{const props=' + to_js(nav_overrides) + ';for(const k in props){try{Object.defineProperty(navigator,k,{get:()=>props[k],configurable:true});}catch(e){}}}catch(e){};})();'
        )

    # screen overrides
    if screen:
        parts.append(
            '(function(){try{const screenProps=' + to_js(screen) + ';for(const k in screenProps){try{Object.defineProperty(screen,k,{get:()=>screenProps[k],configurable:true});}catch(e){}}}catch(e){};})();'
        )

    # navigator.webdriver = false
    parts.append("(function(){try{Object.defineProperty(navigator,'webdriver',{get:()=>false,configurable:true});}catch(e){};})();")

    # simple window.chrome stub
    parts.append("(function(){try{if(!window.chrome)window.chrome={runtime:{}};else window.chrome.runtime=window.chrome.runtime||{};}catch(e){};})();")

    # merge
    return "\\n".join(parts)
