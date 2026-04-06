"""
login.py — Robust login handler

Supports:
  - Cookie-based session reuse (fastest path)
  - Form login (username + password selectors)
  - Multi-step login (separate username / password pages)
  - Post-login verification (check a selector that only appears when logged in)
  - Saving fresh cookies back to DB after login
"""

import logging
import asyncio
from playwright.async_api import Page, BrowserContext
from db import load_cookies, save_cookies

log = logging.getLogger(__name__)


async def ensure_logged_in(
    page: Page,
    context: BrowserContext,
    site: dict,
    db,
) -> bool:
    """
    Ensure the browser is authenticated for `site`.
    Returns True if login succeeded (or not needed), False on failure.
    """
    creds = site.get("credentials")
    if not creds:
        return True   # public site — no login needed

    site_id = site["id"]

    # ── Step 1: Try saved cookies first ──────────────────────────────────────
    saved_cookies = load_cookies(db, site_id)
    if saved_cookies:
        log.info(f"  [{site['name']}] Restoring {len(saved_cookies)} saved cookies.")
        await context.add_cookies(saved_cookies)

        # Verify the session is still valid
        if await _session_still_valid(page, site):
            log.info(f"  [{site['name']}] Existing session valid — skipping login.")
            return True
        else:
            log.info(f"  [{site['name']}] Saved cookies expired — re-logging in.")

    # ── Step 2: Perform fresh login ───────────────────────────────────────────
    success = await _do_login(page, site, creds)
    if not success:
        return False

    # ── Step 3: Save fresh cookies for next run ───────────────────────────────
    fresh_cookies = await context.cookies()
    save_cookies(db, site_id, fresh_cookies)
    log.info(f"  [{site['name']}] Saved {len(fresh_cookies)} cookies for next run.")
    return True


async def _session_still_valid(page: Page, site: dict) -> bool:
    """Check a post-login selector to verify the session is active."""
    verify_url      = site.get("verify_url") or site["url"]
    verify_selector = site.get("verify_selector")

    if not verify_selector:
        return False   # can't verify — assume expired

    try:
        await page.goto(verify_url, wait_until="networkidle", timeout=20_000)
        el = page.locator(verify_selector).first
        return await el.count() > 0 and await el.is_visible()
    except Exception as e:
        log.warning(f"  Session verify failed: {e}")
        return False


async def _do_login(page: Page, site: dict, creds: dict) -> bool:
    """Execute the actual login form flow."""
    login_url = site.get("login_url") or site["url"]
    log.info(f"  Logging in at {login_url}")

    try:
        await page.goto(login_url, wait_until="networkidle", timeout=30_000)
        await asyncio.sleep(1)

        login_type = creds.get("type", "single_page")

        if login_type == "single_page":
            await _fill_single_page(page, creds)

        elif login_type == "two_step":
            # Username on page 1, password on page 2
            await _fill_field(page, creds["username_selector"], creds["username"])
            await _click_and_wait(page, creds.get("next_selector", 'button[type="submit"]'))
            await asyncio.sleep(1.5)
            await _fill_field(page, creds["password_selector"], creds["password"])
            await _click_and_wait(page, creds.get("submit_selector", 'button[type="submit"]'))

        # Wait for navigation after submit
        await page.wait_for_load_state("networkidle", timeout=30_000)
        await asyncio.sleep(2)

        # Verify login succeeded
        success_selector = creds.get("success_selector")
        fail_selector    = creds.get("fail_selector")

        if fail_selector:
            el = page.locator(fail_selector).first
            if await el.count() > 0 and await el.is_visible():
                log.error(f"  Login FAILED — error element visible: {fail_selector}")
                return False

        if success_selector:
            el = page.locator(success_selector).first
            if not (await el.count() > 0 and await el.is_visible()):
                log.error(f"  Login FAILED — success element not found: {success_selector}")
                return False

        log.info("  Login succeeded.")
        return True

    except Exception as e:
        log.error(f"  Login error: {e}")
        return False


async def _fill_single_page(page: Page, creds: dict):
    username_sel = creds.get("username_selector", 'input[name="username"], input[type="email"]')
    password_sel = creds.get("password_selector", 'input[name="password"], input[type="password"]')
    submit_sel   = creds.get("submit_selector",   'button[type="submit"]')

    await _fill_field(page, username_sel, creds["username"])
    await _fill_field(page, password_sel, creds["password"])

    # Checkbox acceptance (e.g. T&C agree)
    checkbox_sel = creds.get("checkbox_selector")
    if checkbox_sel:
        cb = page.locator(checkbox_sel).first
        if await cb.count() > 0 and not await cb.is_checked():
            await cb.check()

    await _click_and_wait(page, submit_sel)


async def _fill_field(page: Page, selector: str, value: str):
    """Try multiple selectors separated by commas."""
    for sel in [s.strip() for s in selector.split(",")]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible(timeout=5_000):
                await el.fill("")
                await el.type(value, delay=60)   # human-like typing speed
                return
        except Exception:
            continue
    raise RuntimeError(f"Could not find field: {selector}")


async def _click_and_wait(page: Page, selector: str):
    for sel in [s.strip() for s in selector.split(",")]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_enabled():
                await el.click()
                return
        except Exception:
            continue
    raise RuntimeError(f"Could not find button: {selector}")
