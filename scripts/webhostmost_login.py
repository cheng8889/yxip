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
        print(
            "Missing required environment variables: " + ", ".join(missing),
            file=sys.stderr,
        )
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
        print(str(exc), file=sys.stderr)
        return TWO_FACTOR_EXIT_CODE
    except TransientPageState as exc:
        print(f"Login attempt encountered a transient issue: {exc}", file=sys.stderr)
        return TRANSIENT_EXIT_CODE
    except Exception as exc:  # noqa: BLE001
        print(f"Login attempt failed: {exc}", file=sys.stderr)
        return 1

    print("Login succeeded")
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


if __name__ == "__main__":
    sys.exit(main())
