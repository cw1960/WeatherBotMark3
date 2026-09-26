# WeatherBotMark3

Algorithmic trading system for Polymarket daily-high-temperature markets.
Ground truth (METAR at the exact ICAO station) is compared against the HRRR model;
Jev System One reads the NWS Area Forecast Discussion; only Fill-Or-Kill limit orders
are ever placed, and only when the dashboard's `trading_active` switch is on.

```
.
├── supabase/migrations/  # run in order in the Supabase SQL editor (001, then 002)
├── bot/                  # Python 3.11 asyncio daemon (US-East Linux VPS)
└── dashboard/            # Next.js 15 control panel (Netlify)
```

## Setup order

1. **Supabase** — create a project, run `supabase/migrations/001_init.sql` then
   `002_all_temperature_markets.sql`, enable Email (magic link) auth, add yourself as a user.
2. **Bot** — see `bot/README.md`. Fill `bot/.env` from `bot/.env.example`.
3. **Dashboard** — `cd dashboard && npm install && cp .env.local.example .env.local && npm run dev`.
   Deploy to Netlify with the two `NEXT_PUBLIC_SUPABASE_*` env vars set.

## Non-negotiables

- Weather data is keyed by `stations.icao_code` only — never a city model. The station is the
  exact airport named in each market's rules (NYC = LaGuardia KLGA, not Central Park).
- Every order is Fill-Or-Kill (`order_type="FOK"` with a price cap); there is no GTC path.
- Secrets live only in git-ignored `.env` files and are read via `os.environ`.
- The bot re-reads `config.trading_active` immediately before every live order;
  when false it writes `decision = 'simulated'` audit rows and never calls Polymarket.
