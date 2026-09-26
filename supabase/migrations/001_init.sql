-- =============================================================================
-- WeatherBotMark3 — Supabase schema
-- Run this in the Supabase SQL editor (Dashboard → SQL → New query).
-- Safe to re-run: every statement is idempotent.
-- =============================================================================

create extension if not exists "pgcrypto";

-- -----------------------------------------------------------------------------
-- Shared trigger: keep updated_at current on any row change
-- -----------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- =============================================================================
-- 1. stations
--    One row per Polymarket weather market we track. The ICAO code is the
--    single source of truth for every weather observation the bot ingests —
--    Polymarket's city name is a marketing label only.
-- =============================================================================
create table if not exists public.stations (
  id                uuid primary key default gen_random_uuid(),
  city_label        text        not null,                 -- e.g. 'New York City' (Polymarket wording)
  icao_code         text        not null,                 -- e.g. 'KNYC' — exact METAR station from contract rules
  nws_office        text,                                 -- e.g. 'OKX' — WFO whose Area Forecast Discussion Jev reads
  nws_gridpoint     text,                                 -- e.g. 'OKX/33,37' — for api.weather.gov hourly forecast
  timezone          text        not null default 'America/New_York',
  polymarket_market_id     text not null,                 -- Polymarket market id (Gamma API)
  polymarket_condition_id  text not null,                 -- CLOB conditionId (0x… hex)
  polymarket_slug          text,                          -- optional, for dashboard deep links
  unit              text        not null default 'F' check (unit in ('F','C')),
  is_active         boolean     not null default true,
  notes             text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),

  constraint stations_icao_format check (icao_code ~ '^[A-Z0-9]{4}$'),
  constraint stations_condition_unique unique (polymarket_condition_id)
);

create index if not exists stations_icao_idx   on public.stations (icao_code);
create index if not exists stations_active_idx on public.stations (is_active) where is_active;

drop trigger if exists stations_set_updated_at on public.stations;
create trigger stations_set_updated_at
  before update on public.stations
  for each row execute function public.set_updated_at();

comment on table  public.stations is 'City → Polymarket market → ICAO METAR station mapping. icao_code is the only key used for weather ingestion.';
comment on column public.stations.icao_code is 'Exact ICAO station named in the Polymarket contract rules (e.g. KNYC). Never a city-wide model.';

-- =============================================================================
-- 2. config
--    Single-row master switch and risk parameters, edited from the dashboard.
--    The bot MUST read trading_active immediately before any FOK order.
-- =============================================================================
create table if not exists public.config (
  id                      smallint primary key default 1 check (id = 1),  -- enforce exactly one row
  trading_active          boolean       not null default false,
  max_budget_usdc         numeric(12,2) not null default 100.00 check (max_budget_usdc >= 0),
  max_order_usdc          numeric(12,2) not null default 25.00  check (max_order_usdc >= 0),
  daily_loss_limit_usdc   numeric(12,2) not null default 50.00  check (daily_loss_limit_usdc >= 0),
  min_divergence_deg      numeric(5,2)  not null default 2.00,  -- |METAR − HRRR| needed to consider a trade
  min_edge_pct            numeric(5,2)  not null default 8.00,  -- required edge vs. market-implied probability
  min_jev_confidence      numeric(4,3)  not null default 0.700 check (min_jev_confidence between 0 and 1),
  alert_divergence_deg    numeric(5,2)  not null default 3.00,  -- push an alert when divergence exceeds this
  alert_drawdown_pct      numeric(5,2)  not null default 20.00, -- alert when daily drawdown exceeds this % of budget
  poll_interval_seconds   integer       not null default 60 check (poll_interval_seconds >= 15),
  updated_by              text,
  updated_at              timestamptz   not null default now()
);

drop trigger if exists config_set_updated_at on public.config;
create trigger config_set_updated_at
  before update on public.config
  for each row execute function public.set_updated_at();

-- Seed the single config row with trading OFF.
insert into public.config (id, trading_active)
values (1, false)
on conflict (id) do nothing;

comment on table  public.config is 'Single-row bot configuration. trading_active=false → bot may only log simulated trades.';
comment on column public.config.trading_active is 'Master kill switch. Bot must re-read this before every live FOK order.';

-- =============================================================================
-- 3. trade_audit_logs
--    Every evaluation cycle that reaches the decision stage writes one row,
--    whether it resulted in a live order, a simulated order, or a skip.
-- =============================================================================
create table if not exists public.trade_audit_logs (
  id                    bigint generated always as identity primary key,
  created_at            timestamptz not null default now(),          -- when the bot logged it
  observed_at           timestamptz,                                 -- METAR observation timestamp
  station_id            uuid references public.stations (id) on delete set null,
  icao_code             text not null,                               -- denormalised for fast querying
  market_id             text not null,
  condition_id          text not null,
  target_date           date,                                        -- the market's resolution date (local)

  -- Ground truth vs model
  metar_temp_f          numeric(5,1),                                -- latest observed temp
  metar_high_so_far_f   numeric(5,1),                                -- running daily high from METAR/MADIS
  hrrr_high_f           numeric(5,1),                                -- HRRR forecast daily high
  nws_forecast_high_f   numeric(5,1),                                -- NWS point forecast high (secondary model)
  divergence_f          numeric(5,1),                                -- metar_high_so_far_f − hrrr_high_f (or as defined by strategy)

  -- Market state at decision time
  market_outcome        text,                                        -- bucket / outcome label we evaluated, e.g. '72-73°F'
  market_price          numeric(6,4),                                -- best ask for that outcome (0–1)
  implied_probability   numeric(6,4),
  model_probability     numeric(6,4),
  edge_pct              numeric(6,2),

  -- Jev System One evaluation of the Area Forecast Discussion
  jev_sentiment         jsonb,                                       -- full structured response from Jev
  jev_confidence        numeric(4,3),                                -- extracted for filtering/plotting
  jev_direction         text check (jev_direction in ('warmer','cooler','neutral','uncertain')),
  afd_issued_at         timestamptz,
  afd_excerpt           text,

  -- Decision & execution
  decision              text not null check (decision in ('skip','simulated','live')),
  skip_reason           text,
  trading_active        boolean not null,                            -- snapshot of the switch at decision time
  order_side            text check (order_side in ('BUY','SELL')),
  order_type            text not null default 'FOK' check (order_type = 'FOK'), -- hard-enforced: FOK only
  order_price           numeric(6,4),
  order_size_usdc       numeric(12,2),
  order_id              text,
  order_status          text,                                        -- 'FILLED' | 'KILLED' | 'REJECTED' | 'SIMULATED' | 'ERROR'
  fill_size             numeric(12,4),
  fill_price            numeric(6,4),
  execution_result      jsonb,                                       -- raw response from polymarket-client
  error_message         text,

  -- Cluster bets: rows placed in the same cycle share a cluster_id
  cluster_id            uuid
);

create index if not exists tal_created_at_idx   on public.trade_audit_logs (created_at desc);
create index if not exists tal_icao_date_idx    on public.trade_audit_logs (icao_code, target_date);
create index if not exists tal_decision_idx     on public.trade_audit_logs (decision);
create index if not exists tal_cluster_idx      on public.trade_audit_logs (cluster_id);
create index if not exists tal_jev_gin_idx      on public.trade_audit_logs using gin (jev_sentiment);

comment on table public.trade_audit_logs is 'Append-only audit trail of every evaluation → decision → (simulated|live) FOK execution.';

-- =============================================================================
-- Row Level Security
--   * Dashboard users (Supabase Auth) read everything and may edit config/stations.
--   * The bot uses the service_role key, which bypasses RLS.
-- =============================================================================
alter table public.stations         enable row level security;
alter table public.config           enable row level security;
alter table public.trade_audit_logs enable row level security;

drop policy if exists "authenticated read stations" on public.stations;
create policy "authenticated read stations"  on public.stations
  for select to authenticated using (true);
drop policy if exists "authenticated write stations" on public.stations;
create policy "authenticated write stations" on public.stations
  for all to authenticated using (true) with check (true);

drop policy if exists "authenticated read config" on public.config;
create policy "authenticated read config"   on public.config
  for select to authenticated using (true);
drop policy if exists "authenticated update config" on public.config;
create policy "authenticated update config" on public.config
  for update to authenticated using (true) with check (true);

drop policy if exists "authenticated read audit" on public.trade_audit_logs;
create policy "authenticated read audit" on public.trade_audit_logs
  for select to authenticated using (true);
-- No insert/update/delete policy for authenticated: only the bot (service_role) writes audit rows.

-- Let the dashboard subscribe to live audit rows and config flips.
do $$
begin
  alter publication supabase_realtime add table public.trade_audit_logs;
exception when duplicate_object then null;
end $$;
do $$
begin
  alter publication supabase_realtime add table public.config;
exception when duplicate_object then null;
end $$;

-- =============================================================================
-- Example station seed (edit condition/market ids to the live contracts)
-- =============================================================================
insert into public.stations
  (city_label, icao_code, nws_office, nws_gridpoint, timezone, polymarket_market_id, polymarket_condition_id, polymarket_slug)
values
  ('New York City', 'KNYC', 'OKX', 'OKX/33,37', 'America/New_York', 'REPLACE_ME', '0xREPLACE_ME_NYC', 'highest-temperature-in-nyc'),
  ('Chicago',       'KORD', 'LOT', 'LOT/65,76', 'America/Chicago',  'REPLACE_ME', '0xREPLACE_ME_CHI', 'highest-temperature-in-chicago')
on conflict (polymarket_condition_id) do nothing;
