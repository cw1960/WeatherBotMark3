-- =============================================================================
-- WeatherBotMark3 — migration 002: trade every Polymarket temperature market
--
-- * stations      → permanent list of all 51 cities (no market ids: those change daily)
-- * market_buckets → NEW: one row per outcome bucket per city per day, refreshed by the bot
-- * trade_audit_logs → unit-neutral temperature columns (°F and °C markets)
--
-- Run once in the Supabase SQL editor after 001_init.sql. Safe to re-run.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. stations: drop per-market columns, add catalog fields
-- -----------------------------------------------------------------------------
alter table public.stations drop constraint if exists stations_condition_unique;
alter table public.stations drop column if exists polymarket_market_id;
alter table public.stations drop column if exists polymarket_condition_id;
alter table public.stations drop column if exists polymarket_slug;
alter table public.stations drop column if exists nws_gridpoint;

alter table public.stations add column if not exists city_slug          text;
alter table public.stations add column if not exists airport_name       text;
alter table public.stations add column if not exists rules_phrase       text;   -- must appear in market rules or we refuse to trade
alter table public.stations add column if not exists latitude           double precision;
alter table public.stations add column if not exists longitude          double precision;
alter table public.stations add column if not exists elevation_m        integer;
alter table public.stations add column if not exists country            text;
alter table public.stations add column if not exists resolution_source  text not null default 'noaa';

do $$ begin
  alter table public.stations add constraint stations_resolution_source_chk
    check (resolution_source in ('noaa','wunderground','hko'));
exception when duplicate_object then null; end $$;

-- The two placeholder rows from 001 are replaced by the verified catalog below.
delete from public.stations where city_slug is null;

create unique index if not exists stations_city_slug_key on public.stations (city_slug);
alter table public.stations alter column city_slug set not null;

comment on column public.stations.icao_code    is 'Exact station named in the contract rules (e.g. KLGA for NYC — NOT Central Park). Verified 2026-09-26.';
comment on column public.stations.rules_phrase is 'Substring the market rules must contain (normalised). Mismatch → bot will not trade that city.';

insert into public.stations
  (city_slug, city_label, icao_code, airport_name, rules_phrase, unit, timezone,
   latitude, longitude, elevation_m, country, resolution_source, nws_office, is_active)
values
  ('nyc', 'New York City', 'KLGA', 'LaGuardia Airport', 'LaGuardia', 'F', 'America/New_York', 40.77945, -73.88027, 9, 'US', 'noaa', 'OKX', true),
  ('atlanta', 'Atlanta', 'KATL', 'Hartsfield-Jackson Intl', 'Hartsfield-Jackson', 'F', 'America/New_York', 33.62972, -84.44223, 309, 'US', 'noaa', 'FFC', true),
  ('miami', 'Miami', 'KMIA', 'Miami Intl', 'Miami Intl Airport', 'F', 'America/New_York', 25.78806, -80.31692, 1, 'US', 'noaa', 'MFL', true),
  ('chicago', 'Chicago', 'KORD', 'O''Hare Intl', 'O''Hare', 'F', 'America/Chicago', 41.96017, -87.93161, 202, 'US', 'noaa', 'LOT', true),
  ('dallas', 'Dallas', 'KDAL', 'Dallas Love Field', 'Love Field', 'F', 'America/Chicago', 32.83836, -96.83584, 148, 'US', 'noaa', 'FWD', true),
  ('austin', 'Austin', 'KAUS', 'Austin-Bergstrom Intl', 'Austin-Bergstrom', 'F', 'America/Chicago', 30.1831, -97.68063, 148, 'US', 'noaa', 'EWX', true),
  ('houston', 'Houston', 'KHOU', 'William P. Hobby Airport', 'Hobby', 'F', 'America/Chicago', 29.64582, -95.28214, 13, 'US', 'noaa', 'HGX', true),
  ('denver', 'Denver', 'KBKF', 'Buckley Space Force Base', 'Buckley', 'F', 'America/Denver', 39.713, -104.758, 1703, 'US', 'noaa', 'BOU', true),
  ('los-angeles', 'Los Angeles', 'KLAX', 'Los Angeles Intl', 'Los Angeles International', 'F', 'America/Los_Angeles', 33.93817, -118.3866, 30, 'US', 'noaa', 'LOX', true),
  ('san-francisco', 'San Francisco', 'KSFO', 'San Francisco Intl', 'San Francisco International', 'F', 'America/Los_Angeles', 37.61961, -122.36561, 2, 'US', 'noaa', 'MTR', true),
  ('seattle', 'Seattle', 'KSEA', 'Seattle-Tacoma Intl', 'Seattle-Tacoma', 'F', 'America/Los_Angeles', 47.44467, -122.31442, 115, 'US', 'noaa', 'SEW', true),
  ('toronto', 'Toronto', 'CYYZ', 'Toronto Pearson Intl', 'Pearson', 'C', 'America/Toronto', 43.679, -79.629, 171, 'CA', 'noaa', null, true),
  ('mexico-city', 'Mexico City', 'MMMX', 'Benito Juárez Intl', 'Benito Juarez', 'C', 'America/Mexico_City', 19.436, -99.072, 2224, 'MX', 'noaa', null, true),
  ('panama-city', 'Panama City', 'MPMG', 'Marcos A. Gelabert Intl', 'Gelabert', 'C', 'America/Panama', 8.967, -79.555, 6, 'PA', 'noaa', null, true),
  ('sao-paulo', 'São Paulo', 'SBGR', 'São Paulo-Guarulhos Intl', 'Guarulhos', 'C', 'America/Sao_Paulo', -23.432, -46.469, 745, 'BR', 'noaa', null, true),
  ('buenos-aires', 'Buenos Aires', 'SAEZ', 'Ministro Pistarini Intl (Ezeiza)', 'Pistarini', 'C', 'America/Argentina/Buenos_Aires', -34.822, -58.536, 16, 'AR', 'noaa', null, true),
  ('london', 'London', 'EGLC', 'London City Airport', 'London City', 'C', 'Europe/London', 51.505, 0.055, 10, 'GB', 'noaa', null, true),
  ('paris', 'Paris', 'LFPB', 'Paris-Le Bourget', 'Le Bourget', 'C', 'Europe/Paris', 48.967, 2.428, 50, 'FR', 'noaa', null, true),
  ('amsterdam', 'Amsterdam', 'EHAM', 'Amsterdam Schiphol', 'Schiphol', 'C', 'Europe/Amsterdam', 52.315, 4.79, -2, 'NL', 'noaa', null, true),
  ('madrid', 'Madrid', 'LEMD', 'Adolfo Suárez Madrid-Barajas', 'Madrid-Barajas', 'C', 'Europe/Madrid', 40.466, -3.555, 589, 'ES', 'noaa', null, true),
  ('munich', 'Munich', 'EDDM', 'Munich Airport', 'Munich Airport', 'C', 'Europe/Berlin', 48.348, 11.813, 445, 'DE', 'noaa', null, true),
  ('milan', 'Milan', 'LIMC', 'Milan Malpensa', 'Malpensa', 'C', 'Europe/Rome', 45.631, 8.728, 221, 'IT', 'noaa', null, true),
  ('warsaw', 'Warsaw', 'EPWA', 'Warsaw Chopin', 'Chopin', 'C', 'Europe/Warsaw', 52.163, 20.961, 107, 'PL', 'noaa', null, true),
  ('helsinki', 'Helsinki', 'EFHK', 'Helsinki-Vantaa', 'Vantaa', 'C', 'Europe/Helsinki', 60.327, 24.957, 56, 'FI', 'noaa', null, true),
  ('moscow', 'Moscow', 'UUWW', 'Vnukovo Intl', 'Vnukovo', 'C', 'Europe/Moscow', 55.592, 37.261, 195, 'RU', 'noaa', null, true),
  ('istanbul', 'Istanbul', 'LTFM', 'Istanbul Airport', 'Istanbul Airport', 'C', 'Europe/Istanbul', 41.262, 28.74, 99, 'TR', 'noaa', null, true),
  ('ankara', 'Ankara', 'LTAC', 'Esenboğa Intl', 'Esenboga', 'C', 'Europe/Istanbul', 40.128, 32.995, 952, 'TR', 'noaa', null, true),
  ('tel-aviv', 'Tel Aviv', 'LLBG', 'Ben Gurion Intl', 'Ben Gurion', 'C', 'Asia/Jerusalem', 32.011, 34.887, 35, 'IL', 'noaa', null, true),
  ('jeddah', 'Jeddah', 'OEJN', 'King Abdulaziz Intl', 'King Abdulaziz', 'C', 'Asia/Riyadh', 21.685, 39.166, 8, 'SA', 'noaa', null, true),
  ('cape-town', 'Cape Town', 'FACT', 'Cape Town Intl', 'Cape Town International', 'C', 'Africa/Johannesburg', -33.965, 18.602, 48, 'ZA', 'noaa', null, true),
  ('karachi', 'Karachi', 'OPKC', 'Jinnah Intl', 'Jinnah', 'C', 'Asia/Karachi', 24.902, 67.139, 20, 'PK', 'noaa', null, true),
  ('lucknow', 'Lucknow', 'VILK', 'Chaudhary Charan Singh Intl', 'Chaudhary Charan Singh', 'C', 'Asia/Kolkata', 26.761, 80.889, 121, 'IN', 'noaa', null, true),
  ('tokyo', 'Tokyo', 'RJTT', 'Tokyo Haneda', 'Haneda', 'C', 'Asia/Tokyo', 35.553, 139.781, 5, 'JP', 'noaa', null, true),
  ('seoul', 'Seoul', 'RKSI', 'Incheon Intl', 'Incheon', 'C', 'Asia/Seoul', 37.469, 126.451, 7, 'KR', 'noaa', null, true),
  ('busan', 'Busan', 'RKPK', 'Gimhae Intl', 'Gimhae', 'C', 'Asia/Seoul', 35.179, 128.938, 3, 'KR', 'noaa', null, true),
  ('beijing', 'Beijing', 'ZBAA', 'Beijing Capital Intl', 'Beijing Capital', 'C', 'Asia/Shanghai', 40.082, 116.603, 31, 'CN', 'noaa', null, true),
  ('shanghai', 'Shanghai', 'ZSPD', 'Shanghai Pudong Intl', 'Pudong', 'C', 'Asia/Shanghai', 31.146, 121.8, 4, 'CN', 'noaa', null, true),
  ('guangzhou', 'Guangzhou', 'ZGGG', 'Guangzhou Baiyun Intl', 'Baiyun', 'C', 'Asia/Shanghai', 23.392, 113.307, 11, 'CN', 'noaa', null, true),
  ('shenzhen', 'Shenzhen', 'ZGSZ', 'Shenzhen Bao''an Intl', 'Shenzhen Bao''an', 'C', 'Asia/Shanghai', 22.639, 113.803, 18, 'CN', 'noaa', null, true),
  ('wuhan', 'Wuhan', 'ZHHH', 'Wuhan Tianhe Intl', 'Tianhe', 'C', 'Asia/Shanghai', 30.783, 114.205, 33, 'CN', 'noaa', null, true),
  ('chongqing', 'Chongqing', 'ZUCK', 'Chongqing Jiangbei Intl', 'Jiangbei', 'C', 'Asia/Shanghai', 29.718, 106.639, 416, 'CN', 'noaa', null, true),
  ('chengdu', 'Chengdu', 'ZUUU', 'Chengdu Shuangliu Intl', 'Shuangliu', 'C', 'Asia/Shanghai', 30.576, 103.95, 494, 'CN', 'noaa', null, true),
  ('qingdao', 'Qingdao', 'ZSQD', 'Qingdao Jiaodong Intl', 'Jiaodong', 'C', 'Asia/Shanghai', 36.362, 120.087, 2, 'CN', 'noaa', null, true),
  ('taipei', 'Taipei', 'RCSS', 'Taipei Songshan', 'Songshan', 'C', 'Asia/Taipei', 25.069, 121.552, 8, 'TW', 'wunderground', null, true),
  ('manila', 'Manila', 'RPLL', 'Ninoy Aquino Intl', 'Ninoy Aquino', 'C', 'Asia/Manila', 14.507, 121.004, 15, 'PH', 'noaa', null, true),
  ('kuala-lumpur', 'Kuala Lumpur', 'WMKK', 'Kuala Lumpur Intl', 'Kuala Lumpur Intl', 'C', 'Asia/Kuala_Lumpur', 2.747, 101.714, 21, 'MY', 'noaa', null, true),
  ('singapore', 'Singapore', 'WSSS', 'Singapore Changi', 'Changi', 'C', 'Asia/Singapore', 1.368, 103.982, 17, 'SG', 'noaa', null, true),
  ('wellington', 'Wellington', 'NZWN', 'Wellington Intl', 'Wellington Intl', 'C', 'Pacific/Auckland', -41.331, 174.806, 12, 'NZ', 'noaa', null, true),
  ('hong-kong', 'Hong Kong', 'VHHH', 'Hong Kong Observatory (HQ, not the airport)', 'Hong Kong Observatory', 'C', 'Asia/Hong_Kong', 22.302, 114.174, 32, 'HK', 'hko', null, false),
  ('jinan', 'Jinan', 'ZSJN', 'Jinan Yaoqiang Intl', null, 'C', 'Asia/Shanghai', 36.856, 117.206, 22, 'CN', 'noaa', null, false),
  ('zhengzhou', 'Zhengzhou', 'ZHCC', 'Zhengzhou Xinzheng Intl', null, 'C', 'Asia/Shanghai', 34.52, 113.834, 149, 'CN', 'noaa', null, false)
on conflict (city_slug) do update set
  city_label = excluded.city_label,
  icao_code = excluded.icao_code,
  airport_name = excluded.airport_name,
  rules_phrase = excluded.rules_phrase,
  unit = excluded.unit,
  timezone = excluded.timezone,
  latitude = excluded.latitude,
  longitude = excluded.longitude,
  elevation_m = excluded.elevation_m,
  country = excluded.country,
  resolution_source = excluded.resolution_source,
  nws_office = excluded.nws_office;
  -- is_active deliberately NOT overwritten on re-run, so dashboard toggles survive.

-- -----------------------------------------------------------------------------
-- 2. market_buckets: today's (and tomorrow's) tradeable outcomes, per city
-- -----------------------------------------------------------------------------
create table if not exists public.market_buckets (
  id              bigint generated always as identity primary key,
  station_id      uuid not null references public.stations (id) on delete cascade,
  target_date     date not null,                 -- local date at the station
  event_id        text not null,
  event_slug      text not null,
  market_id       text not null,
  condition_id    text not null,
  label           text not null,                 -- e.g. '72-73°F', '28°C', '76°F or higher'
  lower_bound     integer,                       -- null = open-ended below
  upper_bound     integer,                       -- null = open-ended above
  unit            text not null check (unit in ('F','C')),
  token_yes       text not null,
  token_no        text not null,
  neg_risk        boolean not null default false,
  rules_ok        boolean not null,              -- rules text names the expected station
  closed          boolean not null default false,
  updated_at      timestamptz not null default now(),
  constraint market_buckets_condition_key unique (condition_id)
);
create index if not exists market_buckets_station_date_idx on public.market_buckets (station_id, target_date);

alter table public.market_buckets enable row level security;
drop policy if exists "authenticated read buckets" on public.market_buckets;
create policy "authenticated read buckets" on public.market_buckets
  for select to authenticated using (true);

comment on table public.market_buckets is 'Refreshed by the bot from the Gamma API. rules_ok=false → never traded.';

-- -----------------------------------------------------------------------------
-- 3. trade_audit_logs: unit-neutral temperature columns (table is empty so far)
-- -----------------------------------------------------------------------------
do $$
declare r record;
begin
  for r in select * from (values
      ('metar_temp_f',        'obs_temp'),
      ('metar_high_so_far_f', 'obs_high_so_far'),
      ('hrrr_high_f',         'model_high'),
      ('nws_forecast_high_f', 'secondary_model_high'),
      ('divergence_f',        'divergence')
    ) as t(old_name, new_name)
  loop
    if exists (select 1 from information_schema.columns
               where table_schema='public' and table_name='trade_audit_logs' and column_name=r.old_name) then
      execute format('alter table public.trade_audit_logs rename column %I to %I', r.old_name, r.new_name);
    end if;
  end loop;
end $$;

alter table public.trade_audit_logs add column if not exists unit        text check (unit in ('F','C'));
alter table public.trade_audit_logs add column if not exists model_name  text;   -- e.g. 'hrrr', 'ecmwf_ifs025'
alter table public.trade_audit_logs add column if not exists token_id    text;
alter table public.trade_audit_logs add column if not exists city_slug   text;
