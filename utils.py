import re
from typing import Optional, Tuple

def parse_proxy(proxy: str | None) -> tuple[Optional[str], Optional[dict]]:
    """Return (server, credentials_dict_or_None) for Playwright/patchright.
    Accepts: scheme://host:port or scheme://user:pass@host:port
    """
    if not proxy:
        return None, None
    # Basic validation
    m = re.match(r'^(?P<scheme>\w+)://(?:(?P<user>[^:@]+):(?P<pwd>[^@]+)@)?(?P<host>[^:]+):(?P<port>\d+)$', proxy.strip())
    if not m:
        raise ValueError("Invalid proxy format. Expected scheme://host:port or scheme://user:pass@host:port")
    d = m.groupdict()
    server = f"{d['scheme']}://{d['host']}:{d['port']}"
    creds = None
    if d.get('user'):
        creds = {"username": d['user'], "password": d['pwd']}
    return server, creds
