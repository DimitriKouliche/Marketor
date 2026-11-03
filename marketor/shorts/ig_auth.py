"""
Turns whatever Instagram token is in .env into one that does not expire.

Graph API Explorer hands out a short-lived user token (an hour or two). Meta's
documented way to a permanent one is two hops: exchange it for a long-lived
user token (60 days), then ask for the Page's token with that — a Page token
minted from a long-lived user token has no expiry. Pasting the Explorer token
straight into .env skips both, and Instagram quietly stops posting the same
day, which is how clip 950 was lost.

Only the Facebook Login flavour (EAA tokens) works this way. The app id comes
from the token's own debug info, so the only extra secret is IG_APP_SECRET.
"""

from __future__ import annotations

import datetime as dt
import os
import re

import requests

from . import env
from .config import Config

GRAPH = "https://graph.facebook.com/v21.0"
ENV_KEY = "IG_ACCESS_TOKEN"


def _debug(token: str) -> dict:
    return requests.get(f"{GRAPH}/debug_token",
                        params={"input_token": token, "access_token": token},
                        timeout=20).json().get("data", {})


def _expiry(info: dict) -> str:
    # A user token can report no expiry yet still lose data access after 90
    # days, which stops publishing just the same; only Page tokens escape it.
    stamp = info.get("expires_at") or (
        info.get("data_access_expires_at") if info.get("type") != "PAGE" else None)
    if not stamp:
        return "never"
    return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _write_env(token: str) -> None:
    path = env.DEFAULT_ENV_PATH
    text = path.read_text(encoding="utf-8")
    line = f"{ENV_KEY}={token}"
    pattern = re.compile(rf"^\s*(export\s+)?{ENV_KEY}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(lambda _: line, text, count=1)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")


def make_permanent(cfg: Config) -> bool:
    """Exchanges IG_ACCESS_TOKEN for a non-expiring Page token and saves it."""
    env.ensure_loaded()
    token = os.environ.get(ENV_KEY, "")
    secret = os.environ.get("IG_APP_SECRET", "")
    if not token:
        print(f"  {ENV_KEY} is not set")
        return False
    if token.startswith("IGAA"):
        print("  IGAA (Instagram Login) tokens cannot become Page tokens; "
              "refresh them via graph.instagram.com/refresh_access_token instead")
        return False
    if not secret:
        print("  IG_APP_SECRET is not set (Meta app > Settings > Basic)")
        return False

    info = _debug(token)
    if not info.get("is_valid"):
        print(f"  current token is not valid: {info.get('error', {}).get('message', info)}")
        print("  generate a fresh one in Graph API Explorer and rerun")
        return False
    if info.get("type") == "PAGE" and not info.get("expires_at"):
        print("  already a non-expiring Page token, nothing to do")
        return True

    print(f"  current: {info.get('type')} token, expires {_expiry(info)}")

    # Hop 1: short-lived user token -> long-lived user token. Harmless on a
    # token that is already long-lived.
    r = requests.get(f"{GRAPH}/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": info["app_id"],
        "client_secret": secret,
        "fb_exchange_token": token,
    }, timeout=20)
    long_user = r.json().get("access_token")
    if not long_user:
        print(f"  exchange failed: {r.text[:200]}")
        return False

    # Hop 2: the Page linked to our Instagram account, by its IG id rather
    # than by guessing which of the operator's Pages is the right one.
    r = requests.get(f"{GRAPH}/me/accounts", params={
        "fields": "name,access_token,instagram_business_account",
        "access_token": long_user,
    }, timeout=20)
    pages = r.json().get("data")
    if pages is None:
        print(f"  listing Pages failed: {r.text[:200]}")
        return False

    target = cfg.publish.instagram_user_id
    page = next((p for p in pages
                 if (p.get("instagram_business_account") or {}).get("id") == target), None)
    if not page:
        names = ", ".join(p.get("name", "?") for p in pages) or "none"
        print(f"  no Page linked to Instagram account {target} (Pages seen: {names})")
        if not pages:
            print("  the token lacks pages_show_list, so Meta lists no Pages; add it "
                  "(and pages_read_engagement) in Graph API Explorer and rerun")
        else:
            print("  the Page shows no linked Instagram account: link it under Page "
                  "settings > Linked accounts, or add business_management if the "
                  "Page sits in a Business Portfolio, then rerun")
        # Still worth keeping: 60 days of posting beats an hour.
        _write_env(long_user)
        os.environ[ENV_KEY] = long_user
        print(f"  saved the long-lived user token meanwhile "
              f"(expires {_expiry(_debug(long_user))})")
        return False

    page_token = page["access_token"]
    page_info = _debug(page_token)
    if not page_info.get("is_valid"):
        print(f"  Page token did not validate: {page_info}")
        return False

    _write_env(page_token)
    os.environ[ENV_KEY] = page_token
    print(f"  saved Page token for '{page['name']}' to .env "
          f"(type {page_info.get('type')}, expires {_expiry(page_info)})")
    return True
