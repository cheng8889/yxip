#!/usr/bin/env python3
"""
Automated login helper for the Webhostmost client area.
The script is designed to be executed within GitHub Actions using Playwright
and relies on credentials passed via environment variables.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from typing import Iterable, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

LOGIN_URL = "https://client.webhostmost.com/clientarea.php"
CLOUDFLARE_TIMEOUT_SECONDS = 30
CLOUDFLARE_POLL_INTERVAL_SECONDS = 2
MAX_ATTEMPTS = 3
TWO_FACTOR_EXIT_CODE = 2
TRANSIENT_EXIT_CODE = 3


class TwoFactorOrCaptchaDetected(Exception):
    """Raised when the login flow encounters a 2FA or CAPTCHA requirement."""


class TransientPageState(Exception):
    """Raised for transient issues (e.g., Cloudflare interstitial) to trigger retries."""


def main() -> int:
    email = os.environ.get("WEBHOSTMOST_EMAIL")
    password = os.environ.get("WEBHOSTMOST_PASSWORD")

    env_pairs = (
        ("WEBHOSTMOST_EMAIL", email),
        ("WEBHOSTMOST_PASSWORD", password),
    )
    missing = [name for name, value in env_pairs if not value]
    if missing:
        message = "Missing required environment variables: " + ", ".join(missing)
        print(message, file=sys.stderr)
        send_telegram_notification(False, email, f"Missing credentials: {', '.join(missing)}")
        return 1

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = None
                try:
                    context = browser.new_context()
                    attempt_login_with_retries(context, email, password)
                finally:
                    if context is not None:
                        context.close()
            finally:
                browser.close()
    except TwoFactorOrCaptchaDetected as exc:
        detail = _compact_text(str(exc)) or "Two-factor or CAPTCHA required."
        print(detail, file=sys.stderr)
        send_telegram_notification(False, email, detail)
        return TWO_FACTOR_EXIT_CODE
    except TransientPageState as exc:
        detail = _compact_text(str(exc)) or "Transient issue encountered."
        print(f"Login attempt encountered a transient issue: {detail}", file=sys.stderr)
        send_telegram_notification(False, email, detail)
        return TRANSIENT_EXIT_CODE
    except Exception as exc:  # noqa: BLE001
        detail = _compact_text(str(exc)) or exc.__class__.__name__
        print(f"Login attempt failed: {detail}", file=sys.stderr)
        send_telegram_notification(False, email, f"Unexpected error: {detail}")
        return 1

    print("Login succeeded")
    send_telegram_notification(True, email, "")
    return 0


def attempt_login_with_retries(context, email: str, password: str) -> None:
    """Attempt the login flow, retrying transient failures a limited number of times."""
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        page = context.new_page()
        try:
            perform_login_flow(page, email, password)
            page.close()
            return
        except TransientPageState as exc:
            last_error = exc
        except Exception:
            page.close()
            raise
        page.close()
        if attempt < MAX_ATTEMPTS:
            time.sleep(3)

    if last_error is not None:
        raise last_error
    raise RuntimeError("Login attempts exhausted without success.")


def perform_login_flow(page, email: str, password: str) -> None:
    navigate_to_login(page)

    if message := detect_twofactor_or_captcha(page):
        raise TwoFactorOrCaptchaDetected(message)

    if not fill_first_available_field(
        page,
        (
            "input[name='username']",
            "input[name='email']",
            "#inputEmail",
            "input[type='email']",
        ),
        email,
    ):
        raise RuntimeError("Unable to locate username/email input field.")

    if not fill_first_available_field(
        page,
        (
            "input[name='password']",
            "#inputPassword",
            "input[type='password']",
        ),
        password,
    ):
        raise RuntimeError("Unable to locate password input field.")

    if not click_first_available(
        page,
        (
            "button[type='submit']",
            "button#login",
            "text=Login",
        ),
    ):
        raise RuntimeError("Unable to locate login submit button.")

    # Give the page a moment to navigate and load.
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        # Not fatal; we will continue to verification and challenges detection.
        pass

    wait_for_cloudflare(page)

    if message := detect_twofactor_or_captcha(page):
        raise TwoFactorOrCaptchaDetected(message)

    if not login_successful(page):
        raise RuntimeError("Login verification failed.")


def navigate_to_login(page) -> None:
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError as exc:
        raise TransientPageState("Timed out navigating to login page.") from exc

    wait_for_cloudflare(page)


def wait_for_cloudflare(page) -> None:
    deadline = time.time() + CLOUDFLARE_TIMEOUT_SECONDS
    while time.time() < deadline:
        if not has_cloudflare_interstitial(page):
            return
        page.wait_for_timeout(CLOUDFLARE_POLL_INTERVAL_SECONDS * 1_000)
    raise TransientPageState("Cloudflare interstitial did not clear within timeout.")


def has_cloudflare_interstitial(page) -> bool:
    try:
        body_text = page.text_content("body", timeout=2_000) or ""
    except PlaywrightTimeoutError:
        return False

    lowered = body_text.lower()
    indicators = (
        "checking your browser",
        "just a moment",
        "please stand by",
        "ddos protection by cloudflare",
    )
    return any(indicator in lowered for indicator in indicators)


def fill_first_available_field(page, selectors: Iterable[str], value: str) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() == 0:
                continue
            target = locator.first
            target.wait_for(state="visible", timeout=5_000)
            target.fill(value)
            return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return False


def click_first_available(page, selectors: Iterable[str]) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() == 0:
                continue
            target = locator.first
            target.wait_for(state="visible", timeout=5_000)
            target.click()
            return True
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return False


def detect_twofactor_or_captcha(page) -> Optional[str]:
    twofactor_selectors = (
        "input[name*='twofactor']",
        "input[name*='2fa']",
        "input[id*='twofactor']",
        "input[id*='2fa']",
        "input[name='token']",
        "input[name='code']",
    )
    captcha_selectors = (
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "div.g-recaptcha",
        "div.h-captcha",
    )

    for selector in twofactor_selectors:
        try:
            if page.locator(selector).count() > 0:
                return "Two-factor authentication challenge detected."
        except Exception:
            continue

    for selector in captcha_selectors:
        try:
            if page.locator(selector).count() > 0:
                return "CAPTCHA challenge detected."
        except Exception:
            continue

    try:
        body_text = page.text_content("body", timeout=2_000) or ""
    except PlaywrightTimeoutError:
        body_text = ""

    lowered = body_text.lower()
    if "two-factor" in lowered or "verification code" in lowered:
        return "Two-factor authentication challenge detected."
    if "captcha" in lowered:
        return "CAPTCHA challenge detected."

    return None


def login_successful(page) -> bool:
    url = page.url.lower()
    if "clientarea" in url and "login" not in url:
        return True

    logout_selectors = (
        "a[href*='logout']",
        "text=Logout",
        "text=Log Out",
    )

    for selector in logout_selectors:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue

    return False


def mask_email(email: Optional[str]) -> str:
    if not email:
        return "<unknown email>"
    stripped = email.strip()
    if not stripped:
        return "<unknown email>"
    if "@" not in stripped:
        return f"{stripped[0]}***" if stripped else "<unknown email>"
    local_part, _, domain = stripped.partition("@")
    if not local_part:
        return f"***@{domain}"
    return f"{local_part[0]}***@{domain}"


def _compact_text(value: str) -> str:
    if not value:
        return ""
    compact = " ".join(value.strip().split())
    return compact[:400]


def send_telegram_notification(success: bool, email: Optional[str], reason: str) -> None:
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    masked = mask_email(email)

    if success:
        text = f"Webhostmost login succeeded at {timestamp} for {masked}."
    else:
        detail = _compact_text(reason)
        if detail:
            text = f"Webhostmost login failed at {timestamp} for {masked}: {detail}"
        else:
            text = f"Webhostmost login failed at {timestamp} for {masked}."

    send_tg(text)


def send_tg(text: str) -> bool:
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")

    if not (token and chat_id):
        return False

    try:
        import requests
    except ImportError:
        print(
            "Telegram notification skipped: requests library unavailable",
            file=sys.stderr,
        )
        return False

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }

    try:
        response = requests.post(api_url, data=payload, timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(
            f"Telegram notification failed: {exc.__class__.__name__}",
            file=sys.stderr,
        )
        return False

    if response.status_code >= 400:
        print(f"Telegram notification failed: HTTP {response.status_code}", file=sys.stderr)
        return False

    try:
        response_json = response.json()
    except Exception:  # noqa: BLE001
        response_json = None

    if isinstance(response_json, dict) and not response_json.get("ok", True):
        description = _compact_text(str(response_json.get("description", "")))
        if description:
            print(f"Telegram notification failed: {description}", file=sys.stderr)
        else:
            print("Telegram notification failed: Telegram API returned ok=false", file=sys.stderr)
        return False

    print("Telegram notification sent")
    return True


if __name__ == "__main__":
    sys.exit(main())
