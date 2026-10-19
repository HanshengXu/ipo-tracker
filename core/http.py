"""Shared HTTP plumbing for every adapter.

The central idea: a `requests.Session` keeps a cookie jar across requests the
way a browser does. Bot-protection layers (Incapsula on set.or.th, Cloudflare
elsewhere) hand out clearance cookies when you load a normal page. Load the
page first, keep the session, then call the JSON endpoint on that same session.

Never hardcode cookies copied out of a browser. They expire within hours, so
the cron would work today and silently break by Wednesday -- and a cookie in a
public repo is a leak.
"""

from __future__ import annotations

import time

import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)


def warm_session(
    page_url: str,
    *,
    user_agent: str = DEFAULT_UA,
    timeout: int = 30,
    pause: float = 1.0,
) -> requests.Session:
    """Load a normal page to collect bot-protection cookies, return the session.

    `pause` is politeness, not superstition: hammering an endpoint the
    millisecond after the page load is the pattern rate limiters look for.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"})
    r = s.get(page_url, timeout=timeout)
    r.raise_for_status()
    time.sleep(pause)
    return s


def get_json(
    session: requests.Session,
    url: str,
    *,
    referer: str,
    params: dict | None = None,
    timeout: int = 30,
) -> dict:
    """GET a JSON endpoint and fail LOUDLY if it doesn't return JSON.

    This guard matters more than it looks. When bot protection kicks in, the
    server returns 200 OK with an HTML challenge page. Without the content-type
    check, `r.json()` raises a confusing decode error deep in the stack, or
    worse, a lenient parser returns something empty and the pipeline writes a
    file with zero rows and no complaint. Explicit beats silent.
    """
    r = session.get(
        url,
        params=params,
        headers={"Accept": "application/json, text/plain, */*", "Referer": referer},
        timeout=timeout,
    )
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "json" not in ctype.lower():
        raise RuntimeError(
            f"Expected JSON from {r.url}, got content-type {ctype!r}. "
            f"First 200 chars: {r.text[:200]!r}"
        )
    return r.json()
