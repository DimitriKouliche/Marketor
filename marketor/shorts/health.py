"""
Live checks on the things that fail silently.

`publish --check` used to inspect credentials without ever calling anything,
which is how Instagram reported "ok" for three days after Meta deactivated the
app. A credential that exists and a credential that works are different facts,
and only the second one matters at 17:00 with nobody watching.

Two failure modes get special attention because neither announces itself:

- A platform token revoked upstream. The post fails, the pipeline records it,
  and nothing tells you until you look.
- An expired LLM key. This one is worse: the copywriter falls back to
  deterministic templates and keeps publishing, so the feed degrades to
  identical captions rather than stopping.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

import requests

from . import env
from .config import Config

#: Warn this far ahead of a known expiry, so there is time to act.
EXPIRY_WARNING_DAYS = 7


def _instagram(cfg: Config) -> tuple[str, str]:
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        return "fail", "IG_ACCESS_TOKEN absent"
    user_id = cfg.publish.instagram_user_id
    if not user_id:
        return "fail", "publish.instagram_user_id vide"

    host = "https://graph.instagram.com" if token.startswith("IGAA") else "https://graph.facebook.com"
    try:
        r = requests.get(
            f"{host}/v21.0/{user_id}/content_publishing_limit",
            params={"fields": "quota_usage,config", "access_token": token}, timeout=20,
        )
    except Exception as exc:
        return "fail", f"reseau: {exc}"

    body = r.json()
    if "error" in body:
        return "fail", body["error"].get("message", "")[:110]

    used = (body.get("data") or [{}])[0].get("quota_usage", "?")
    note = f"quota {used} utilise"

    # Expiry is only knowable on the Facebook flavour, which needs the app
    # secret to introspect; the token's own debug endpoint accepts itself.
    try:
        d = requests.get(f"{host}/v21.0/debug_token",
                         params={"input_token": token, "access_token": token},
                         timeout=20).json().get("data", {})
        if d.get("expires_at"):
            when = dt.datetime.fromtimestamp(d["expires_at"], dt.timezone.utc)
            days = (when - dt.datetime.now(dt.timezone.utc)).days
            note += f", expire dans {days}j"
            if days <= EXPIRY_WARNING_DAYS:
                return "warn", note + " — a renouveler"
    except Exception:
        pass
    return "ok", note


def _youtube(cfg: Config) -> tuple[str, str]:
    if not cfg.publish.youtube_token.exists():
        return "warn", "pas encore autorise (le prochain post ouvre un navigateur)"
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from .publishers.youtube import SCOPES

        creds = Credentials.from_authorized_user_file(str(cfg.publish.youtube_token), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())  # exercises the refresh path, no upload
        return ("ok", "jeton valide") if creds.valid else ("fail", "jeton invalide")
    except Exception as exc:
        return "fail", str(exc)[:110]


def _tiktok(cfg: Config) -> tuple[str, str]:
    path = cfg.publish.tiktok_token
    if not path.exists():
        return "fail", "tiktok_token.json absent (lancer 'marketor tiktok-auth')"
    try:
        from .publishers.tiktok import TikTokPublisher

        publisher = TikTokPublisher()
        info = publisher._creator_info(publisher._token(cfg))
    except Exception as exc:
        return "fail", str(exc)[:110]

    options = info.get("privacy_level_options") or []
    sandbox = os.environ.get("TIKTOK_CLIENT_KEY", "").startswith("sbaw")
    where = "SANDBOX" if sandbox else "production"
    if options and options != ["SELF_ONLY"]:
        return "ok", f"{where}, publication publique disponible"
    return "warn", f"{where}, audit non passe — tout post force en SELF_ONLY"


def _llm(cfg: Config) -> list[tuple[str, str, str]]:
    """The writer and planner keys. A dead key degrades output, it does not stop it."""
    from .copywriter import PROVIDERS

    out = []
    for role, provider, model in (
        ("redacteur", cfg.copy.provider, cfg.copy.model),
        ("planificateur", cfg.orchestrator.provider, cfg.orchestrator.model),
    ):
        preset = PROVIDERS.get(provider, PROVIDERS["openai"])
        key = os.environ.get(preset["api_key_env"])
        if not key:
            out.append((role, "fail", f"{preset['api_key_env']} absent — legendes en gabarit"))
            continue
        try:
            from openai import OpenAI

            client = OpenAI(api_key=key, base_url=preset["base_url"])
            client.models.list()
            out.append((role, "ok", f"{provider}/{model}"))
        except Exception as exc:
            out.append((role, "fail", f"{provider}: {str(exc)[:90]}"))
    return out


def report(cfg: Config) -> bool:
    """Prints a live health report. Returns False if anything failed."""
    env.ensure_loaded()
    marks = {"ok": "ok  ", "warn": "!!  ", "fail": "XX  "}
    healthy = True

    print("Plateformes (appels reels):")
    checks = {"youtube": _youtube, "tiktok": _tiktok, "instagram": _instagram}
    for platform in cfg.publish.platforms:
        check = checks.get(platform)
        if check is None:
            print(f"  ??  {platform:<14}pas de controle")
            continue
        state, note = check(cfg)
        manual = platform in cfg.orchestrator.manual_platforms
        suffix = "  [exclu du planificateur]" if manual else ""
        print(f"  {marks[state]}{platform:<14}{note}{suffix}")
        if state == "fail" and not manual:
            healthy = False

    print("")
    print("Modeles:")
    for role, state, note in _llm(cfg):
        print(f"  {marks[state]}{role:<14}{note}")
        if state == "fail":
            healthy = False

    return healthy
