# Tender Agent — Production Setup Guide

Automated tender scraper with login support, pagination, AI extraction,
Google Sheets output, CSV backup, deduplication, and cron scheduling.

---

## Project Structure

```
tender-agent/
├── scraper.py          # Main scraper (run this)
├── cron_setup.py       # Cron job installer
├── notify.py           # Email digest module
├── requirements.txt    # Python dependencies
├── config/
│   ├── sites.json      # Your site configurations  ← edit this
│   └── google_credentials.json   # Google service account (optional)
├── output/
│   ├── tenders.csv     # All tenders (appended on each run)
│   └── seen_ids.json   # Deduplication store (auto-created)
└── logs/
    ├── scraper.log     # Detailed run logs
    └── cron.log        # Cron stdout/stderr
```

---

## 1. Install Dependencies

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install Python packages
pip install -r requirements.txt

# Install Playwright browser (Chromium)
playwright install chromium
```

---

## 2. Set Your API Key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."

# Add to shell profile so it persists:
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.bashrc
```

---

## 3. Configure Your Sites — `config/sites.json`

Each site entry supports:

| Field | Required | Description |
|---|---|---|
| `name` | yes | Display name for logs/output |
| `url` | yes | Starting URL of the tender listing page |
| `login_url` | no | URL of the login page (if different from `url`) |
| `credentials` | no | Login details (see below) |
| `wait_for_selector` | no | CSS selector to wait for before scraping |
| `pagination` | no | Pagination config (see below) |
| `delay_ms` | no | Milliseconds to wait between page loads (default: 2000) |

### Login Configuration

```json
"credentials": {
  "username": "your@email.com",
  "password": "yourpassword",
  "username_selector": "input[name='email']",
  "password_selector": "input[name='password']",
  "submit_selector":   "button[type='submit']"
}
```

> **Security tip:** Store passwords in environment variables, not directly in sites.json:
> ```json
> "username": "${PORTAL_USER}",
> "password": "${PORTAL_PASS}"
> ```
> Then export `PORTAL_USER` and `PORTAL_PASS` before running.

### Pagination Strategies

**No pagination** (single page):
```json
"pagination": { "strategy": "none" }
```

**Next button** (click "Next page" link):
```json
"pagination": {
  "strategy": "next_button",
  "next_selector": "a.pagination-next",
  "max_pages": 10
}
```

**URL parameter** (page=1, page=2, ...):
```json
"pagination": {
  "strategy": "url_param",
  "param": "page",
  "start": 1,
  "increment": 1,
  "max_pages": 20
}
```

---

## 4. Run Manually (Test First)

```bash
cd tender-agent
python scraper.py
```

Check `output/tenders.csv` and `logs/scraper.log`.

---

## 5. Set Up Cron (Automated Scheduling)

```bash
# Install daily 9am cron job
python cron_setup.py install

# Other commands:
python cron_setup.py show     # see current crontab
python cron_setup.py remove   # remove the job
python cron_setup.py run      # run once immediately
```

**Change the schedule** — edit `CHOSEN_SCHEDULE` in `cron_setup.py`:

| Key | Schedule |
|---|---|
| `hourly` | Every hour |
| `6h` | Every 6 hours |
| `daily_6am` | Daily at 6:00 AM |
| `daily_9am` | Daily at 9:00 AM |
| `weekdays` | Weekdays at 8:00 AM |

---

## 6. Google Sheets Output (Optional)

### Create a Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project → Enable **Google Sheets API** and **Google Drive API**
3. Create a **Service Account** → Download JSON key
4. Save the JSON key as `config/google_credentials.json`
5. Share your Google Sheet with the service account email (Editor access)

### Enable in scraper.py

Edit the `DEFAULT_CONFIG` in `scraper.py`:

```python
"google_sheets": {
    "enabled": True,
    "credentials_file": "config/google_credentials.json",
    "spreadsheet_id": "YOUR_SHEET_ID_FROM_URL",
    "worksheet_name": "Tenders",
},
```

The Sheet ID is in the URL:
`https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`

---

## 7. Email Notifications (Optional)

Set these environment variables to receive a daily HTML digest:

```bash
export NOTIFY_EMAIL_TO="you@company.com"
export NOTIFY_EMAIL_FROM="tenderbot@gmail.com"
export NOTIFY_SMTP_HOST="smtp.gmail.com"
export NOTIFY_SMTP_PORT="587"
export NOTIFY_SMTP_USER="tenderbot@gmail.com"
export NOTIFY_SMTP_PASS="your-app-password"   # Gmail App Password
```

Then call `send_digest()` at the end of your run:

```python
from notify import send_digest
# at bottom of main():
send_digest(all_new)
```

For Gmail, use an **App Password** (not your regular password):
Settings → Security → 2-Step Verification → App Passwords

---

## 8. Deploying to a Server (Always-On)

If you don't have a server, the cheapest options are:

| Platform | Cost | Notes |
|---|---|---|
| **Railway** | ~$5/month | Easy deploy, set env vars in dashboard |
| **Render** | Free tier | Cron jobs built-in |
| **AWS EC2** | ~$8/month | t3.micro, full control |
| **GitHub Actions** | Free | Runs on schedule, no server needed |

### GitHub Actions (Free, No Server)

Create `.github/workflows/tender-scraper.yml`:

```yaml
name: Tender Scraper
on:
  schedule:
    - cron: '0 9 * * 1-5'   # weekdays 9am UTC
  workflow_dispatch:          # allow manual trigger

jobs:
  scrape:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt
      - run: playwright install chromium
      - run: python scraper.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      - uses: actions/upload-artifact@v4
        with:
          name: tenders-csv
          path: output/tenders.csv
```

Add `ANTHROPIC_API_KEY` in GitHub → Settings → Secrets.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Login fails | Inspect the login page, copy exact CSS selectors for fields |
| No tenders extracted | Check `logs/scraper.log` — may need `wait_for_selector` |
| Rate limited / blocked | Increase `delay_ms` to 5000+; use a residential proxy |
| CAPTCHA on login | Use Playwright with `headless=False` to solve once, then save cookies |
| Google Sheets fails | Confirm service account email has Editor access on the sheet |

---

## Saving Login Cookies (Advanced)

For sites with persistent sessions, save cookies after first manual login:

```python
# save_cookies.py — run once manually
import asyncio, json
from playwright.async_api import async_playwright

async def save():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # visible!
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("https://your-portal.com/login")
        input("Log in manually, then press Enter here...")
        cookies = await ctx.cookies()
        json.dump(cookies, open("config/cookies.json", "w"))
        await browser.close()

asyncio.run(save())
```

Then load cookies in `scraper.py` before navigation:
```python
cookies = json.load(open("config/cookies.json"))
await context.add_cookies(cookies)
```
