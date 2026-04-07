# Tender Agent v2 — Full Stack Setup

Python scraper + Supabase backend + Lovable frontend.

```
tender-agent-v2/
├── scraper/
│   ├── main.py            # CLI: scrape all sites now
│   ├── worker.py          # Long-running queue consumer (deploy this to Railway)
│   ├── db.py              # All Supabase read/write operations
│   ├── browser_pool.py    # Concurrent tab manager (MAX_TABS env var)
│   ├── login.py           # Login handler (cookie reuse, form, two-step)
│   ├── pagination.py      # 6 pagination strategies
│   ├── extractor.py       # Claude AI extraction + chunking
│   ├── notify.py          # Email digest
│   ├── requirements.txt
│   ├── Dockerfile
│   └── sites_examples.json
└── supabase/
    ├── schema.sql                        # Run this first in Supabase SQL editor
    └── functions/run-scraper/index.ts    # Edge Function for Lovable button
```

---

## Step 1 — Supabase Database

1. Open your Supabase project → **SQL Editor** → **New Query**
2. Paste the entire contents of `supabase/schema.sql`
3. Click **Run**

This creates: `sites`, `tenders`, `scrape_runs`, `site_cookies`, `scrape_queue`.

**Get your keys** from Supabase → Settings → API:
- `SUPABASE_URL` — your project URL
- `SUPABASE_SERVICE_KEY` — the `service_role` secret key (not the anon key)

---

## Step 2 — Deploy Edge Function

```bash
# Install Supabase CLI
npm install -g supabase

# Login
supabase login

# Link to your project (get ref from Supabase dashboard URL)
supabase link --project-ref YOUR_PROJECT_REF

# Deploy
supabase functions deploy run-scraper

# Set the service role secret inside the function
supabase secrets set SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
```

---

## Step 3 — Deploy Worker to Railway

1. Go to **railway.app** → New Project → Deploy from GitHub
2. Point it at your repo (push this folder to GitHub first)
3. Set the **Root Directory** to `scraper/`
4. Railway auto-detects the `Dockerfile`
5. Add these **Environment Variables** in Railway dashboard:

```
ANTHROPIC_API_KEY      = sk-ant-...
SUPABASE_URL           = https://xxxx.supabase.co
SUPABASE_SERVICE_KEY   = eyJ...
MAX_BROWSER_TABS       = 5        # concurrent tabs (increase for more sites)
POLL_INTERVAL_SECONDS  = 30       # how often to check for queued jobs
```

6. The worker starts automatically and polls for jobs every 30 seconds.

**For scheduled scraping** (without button clicks), add a Railway Cron:
- Settings → Cron → `0 9 * * 1-5` (weekdays 9am)
- Command: `python main.py`

---

## Step 4 — Add Your Sites via Lovable / Supabase

Either use the Lovable Settings page (once built) or insert directly:

```sql
insert into sites (name, url, pagination, credentials, delay_ms) values (
  'My Tender Portal',
  'https://portal.example.com/tenders',
  '{"strategy":"url_param","param":"page","start":1,"increment":1,"max_pages":10}',
  '{"type":"single_page","username":"me@email.com","password":"secret","username_selector":"input[name=email]","password_selector":"input[name=password]","submit_selector":"button[type=submit]"}',
  2500
);
```

See `sites_examples.json` for all pagination and login patterns.

---

## Step 5 — Wire Lovable to Supabase

In your Lovable project, type:

> *"Connect to Supabase. Read from the `tenders` table and display them in the dashboard table. Subscribe to realtime updates so new tenders appear automatically. The 'Run Scraper' button should call the Supabase Edge Function at `/functions/v1/run-scraper` with the user's auth token."*

> *"Add a Settings page where I can view, add, edit, and delete rows in the `sites` table. Show the `scrape_runs` table as a run history log. Show the `scrape_queue` table status."*

---

## Local Testing

```bash
cd scraper
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

export ANTHROPIC_API_KEY="sk-ant-..."
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_SERVICE_KEY="eyJ..."

# Dry run (no DB writes)
python main.py --dry-run

# Scrape one specific site
python main.py --site "UK Find a Tender"

# Start the queue worker locally
python worker.py
```

---

## Pagination Strategy Reference

| Strategy | Use when |
|---|---|
| `none` | Single page, all tenders visible |
| `url_param` | URL has `?page=1`, `?p=2`, `?offset=20` etc |
| `next_button` | "Next page" link or button |
| `load_more` | "Load more results" button |
| `infinite` | Page extends as you scroll down |
| `tab_urls` | Multiple category URLs to visit in sequence |

Strategies can be combined: set `tab_urls` to visit each category tab, and set a `pagination` strategy to paginate within each tab.

---

## Tuning for Many Sites

- **Increase MAX_BROWSER_TABS** to scrape more sites in parallel (use 8–10 on Railway Hobby plan)
- **Increase Railway RAM** if you hit memory errors with many tabs (2GB+ recommended)
- **Add delays** (`delay_ms: 4000+`) on rate-limited portals
- **Cookie reuse** means login only runs once per session — subsequent runs are fast
- The **fingerprint deduplication** ensures re-running the scraper never creates duplicate rows



