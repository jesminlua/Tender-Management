#!/usr/bin/env python3
"""
cron_setup.py  —  Install the tender scraper as a system cron job.

Usage:
    python cron_setup.py install   # add to crontab
    python cron_setup.py remove    # remove from crontab
    python cron_setup.py show      # print current crontab
    python cron_setup.py run       # run once immediately (for testing)
"""

import sys
import os
import subprocess
import textwrap
from pathlib import Path

PROJECT_DIR  = Path(__file__).parent.resolve()
PYTHON       = sys.executable
SCRAPER      = PROJECT_DIR / "scraper.py"
LOG_FILE     = PROJECT_DIR / "logs" / "cron.log"
CRON_MARKER  = "# tender-agent"

# ── Schedule options ─────────────────────────────────────────────────────────
SCHEDULES = {
    "hourly":    "0 * * * *",
    "6h":        "0 */6 * * *",
    "daily_6am": "0 6 * * *",
    "daily_9am": "0 9 * * *",
    "weekdays":  "0 8 * * 1-5",
}

# Change this to any key above
CHOSEN_SCHEDULE = "daily_9am"

# ─────────────────────────────────────────────────────────────────────────────

def cron_line() -> str:
    schedule = SCHEDULES[CHOSEN_SCHEDULE]
    cmd = (
        f"cd {PROJECT_DIR} && "
        f"ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY "
        f"{PYTHON} {SCRAPER} >> {LOG_FILE} 2>&1"
    )
    return f"{schedule} {cmd} {CRON_MARKER}"


def get_current_crontab() -> str:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout


def set_crontab(content: str):
    proc = subprocess.run(["crontab", "-"], input=content, text=True)
    if proc.returncode != 0:
        print("ERROR: crontab update failed.")
        sys.exit(1)


def install():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    current = get_current_crontab()

    if CRON_MARKER in current:
        print("Cron job already installed. Run 'remove' first to update it.")
        return

    new_line = cron_line()
    updated  = current.rstrip("\n") + "\n" + new_line + "\n"
    set_crontab(updated)
    print(f"Cron job installed ({CHOSEN_SCHEDULE}):")
    print(f"  {new_line}")


def remove():
    current = get_current_crontab()
    if CRON_MARKER not in current:
        print("No tender-agent cron job found.")
        return
    lines   = [l for l in current.splitlines() if CRON_MARKER not in l]
    updated = "\n".join(lines) + "\n"
    set_crontab(updated)
    print("Cron job removed.")


def show():
    print(get_current_crontab() or "(empty crontab)")


def run_once():
    print(f"Running scraper once: {PYTHON} {SCRAPER}")
    os.chdir(PROJECT_DIR)
    result = subprocess.run([PYTHON, str(SCRAPER)])
    sys.exit(result.returncode)


COMMANDS = {
    "install": install,
    "remove":  remove,
    "show":    show,
    "run":     run_once,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "install"
    if cmd not in COMMANDS:
        print(f"Unknown command '{cmd}'. Use: {', '.join(COMMANDS)}")
        sys.exit(1)
    COMMANDS[cmd]()
