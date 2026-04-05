-- ============================================================
-- Tender Agent — Supabase Database Schema
-- Run this in Supabase → SQL Editor → New Query → Run
-- ============================================================

-- Enable UUID generation
create extension if not exists "pgcrypto";


-- ── SITES ────────────────────────────────────────────────────
create table if not exists sites (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  url         text not null,
  login_url   text,
  enabled     boolean not null default true,
  credentials jsonb,
  pagination  jsonb,
  tab_urls    jsonb,
  wait_for_selector text,
  verify_url        text,
  verify_selector   text,
  delay_ms          int not null default 2000,
  created_at  timestamptz default now(),
  updated_at  timestamptz default now()
);

alter table sites enable row level security;
create policy "authenticated users can manage sites"
  on sites for all using (auth.role() = 'authenticated');


-- ── TENDERS ──────────────────────────────────────────────────
create table if not exists tenders (
  id          uuid primary key default gen_random_uuid(),
  fingerprint text unique not null,
  site_id     uuid references sites(id) on delete set null,
  source_site text,
  title       text,
  reference   text,
  issuer      text,
  category    text,
  deadline    text,
  budget      text,
  status      text,
  description text,
  url         text,
  location    text,
  contact     text,
  scraped_at  timestamptz default now(),
  created_at  timestamptz default now()
);

create index if not exists tenders_status_idx     on tenders(status);
create index if not exists tenders_site_idx       on tenders(site_id);
create index if not exists tenders_scraped_at_idx on tenders(scraped_at desc);
create index if not exists tenders_deadline_idx   on tenders(deadline);

alter table tenders enable row level security;
create policy "authenticated users can read tenders"
  on tenders for select using (auth.role() = 'authenticated');
create policy "service role can write tenders"
  on tenders for all using (auth.role() = 'service_role');


-- ── SCRAPE RUNS ───────────────────────────────────────────────
create table if not exists scrape_runs (
  id              uuid primary key default gen_random_uuid(),
  site_id         uuid references sites(id) on delete set null,
  status          text not null default 'running',
  started_at      timestamptz default now(),
  finished_at     timestamptz,
  pages_scraped   int default 0,
  tenders_found   int default 0,
  error_message   text
);

alter table scrape_runs enable row level security;
create policy "authenticated users can read runs"
  on scrape_runs for select using (auth.role() = 'authenticated');
create policy "service role can write runs"
  on scrape_runs for all using (auth.role() = 'service_role');


-- ── SITE COOKIES ─────────────────────────────────────────────
create table if not exists site_cookies (
  site_id     uuid primary key references sites(id) on delete cascade,
  cookies     text not
