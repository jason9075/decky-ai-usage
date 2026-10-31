"""
AI Usage - Decky plugin backend.

Reads the OAuth credentials that Claude Code and the Codex CLI already keep on
disk and asks each service for the current rate-limit windows. Ported from
https://github.com/jason9075/ai-usage-cli, using only the standard library
because Decky's bundled Python has no third-party packages.

Credentials are only ever read, never refreshed or written back: refreshing
here would rotate the refresh token underneath the real CLI and log it out.
When a token has expired, running the CLI once refreshes it.
"""

import asyncio
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import decky

CACHE_TTL_SECONDS = 120
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def _ssl_context() -> ssl.SSLContext:
    # Decky's frozen Python may not find the system certificates on its own.
    if os.path.exists(CA_BUNDLE):
        return ssl.create_default_context(cafile=CA_BUNDLE)
    return ssl.create_default_context()


def _http_get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode())


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _seconds_until(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())


def _result(provider: str) -> dict:
    return {"provider": provider, "plan": "", "email": "", "windows": [], "cost": None, "error": None}


def _window(label: str, used_percent: float, resets_in: float | None) -> dict:
    return {"label": label, "used_percent": round(float(used_percent), 1), "resets_in": resets_in}


def _http_error(result: dict, e: Exception, expired_hint: str) -> dict:
    if isinstance(e, urllib.error.HTTPError):
        result["error"] = expired_hint if e.code == 401 else f"HTTP {e.code}"
    else:
        result["error"] = f"Network error: {getattr(e, 'reason', e)}"
    return result


def fetch_claude(home: Path) -> dict:
    result = _result("Claude")
    creds = _read_json(home / ".claude" / ".credentials.json")
    oauth = (creds or {}).get("claudeAiOauth", {})
    token = oauth.get("accessToken")
    if not token:
        result["error"] = "Not signed in (no ~/.claude/.credentials.json)"
        return result

    tier = f"{oauth.get('rateLimitTier') or ''} {oauth.get('subscriptionType') or ''}".lower()
    for plan in ("max", "pro", "team", "enterprise", "free"):
        if plan in tier:
            result["plan"] = plan.title()
            break
    result["email"] = oauth.get("email", "")

    try:
        data = _http_get_json(
            "https://api.anthropic.com/api/oauth/usage",
            {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        )
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return _http_error(result, e, "Token expired - run `claude` once to refresh")

    for key, label in [
        ("five_hour", "Session (5h)"),
        ("seven_day", "Weekly (7d)"),
        ("seven_day_sonnet", "Sonnet (7d)"),
        ("seven_day_opus", "Opus (7d)"),
    ]:
        window = data.get(key) or {}
        if window.get("utilization") is None:
            continue
        result["windows"].append(
            _window(label, window["utilization"], _seconds_until(window.get("resets_at")))
        )

    # Amounts are in cents; non-enterprise plans sometimes report another 100x.
    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled") is True and extra.get("used_credits") is not None and extra.get("monthly_limit") is not None:
        used, limit = extra["used_credits"] / 100.0, extra["monthly_limit"] / 100.0
        if result["plan"] != "Enterprise" and limit >= 1000:
            used, limit = used / 100.0, limit / 100.0
        result["cost"] = {"used": used, "limit": limit, "currency": extra.get("currency") or "USD"}
    return result


def fetch_codex(home: Path) -> dict:
    result = _result("Codex")
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    tokens = (_read_json(codex_home / "auth.json") or {}).get("tokens", {})
    token = tokens.get("access_token")
    if not token:
        result["error"] = "Not signed in (no ~/.codex/auth.json)"
        return result

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "decky-ai-usage"}
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]
    try:
        data = _http_get_json("https://chatgpt.com/backend-api/wham/usage", headers)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return _http_error(result, e, "Token expired - run `codex` once to refresh")

    result["email"] = data.get("email", "")
    result["plan"] = data.get("plan_type", "")
    for section_key, prefix in [("rate_limit", ""), ("code_review_rate_limit", "Review ")]:
        section = data.get(section_key)
        if not isinstance(section, dict):
            continue
        for win_key, label in [("primary_window", "Session (5h)"), ("secondary_window", "Weekly (7d)")]:
            window = section.get(win_key)
            if not isinstance(window, dict):
                continue
            resets = window.get("reset_after_seconds")
            result["windows"].append(
                _window(f"{prefix}{label}", window.get("used_percent", 0), resets if resets else None)
            )
    return result


PROVIDERS = [fetch_claude, fetch_codex]


class Plugin:
    async def _main(self):
        self.cache: dict | None = None
        self.lock = asyncio.Lock()
        decky.logger.info("AI Usage loaded")

    async def _unload(self):
        decky.logger.info("AI Usage unloaded")

    async def get_usage(self, force: bool = False) -> dict:
        async with self.lock:
            if not force and self.cache and time.time() - self.cache["updated_at"] < CACHE_TTL_SECONDS:
                return self.cache

            home = Path(decky.DECKY_USER_HOME)
            results = await asyncio.gather(
                *(asyncio.to_thread(fetch, home) for fetch in PROVIDERS), return_exceptions=True
            )
            providers = []
            for fetch, res in zip(PROVIDERS, results):
                if isinstance(res, BaseException):
                    decky.logger.exception("Provider %s failed", fetch.__name__, exc_info=res)
                    error = str(res) or "Unexpected error"
                    res = _result(fetch.__name__.removeprefix("fetch_").title())
                    res["error"] = error
                providers.append(res)

            self.cache = {"providers": providers, "updated_at": time.time()}
            return self.cache
