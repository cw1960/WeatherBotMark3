# /bot — execution daemon

```
bot/
├── main.py                     # asyncio daemon: discover → observe → evaluate → gate → FOK → audit
├── discover_markets.py         # report: every city's markets today/tomorrow + live METAR bucket
├── edge_report.py              # report: every model per city, bucket probabilities vs prices
├── backtest.py                 # replay past settled markets → score + learn per-station calibration
├── peak_times.py               # when does each city's daily high happen? → calibration/peak_times.json
├── calibration/                # station_calibration.json (used by the bot) + backtest_report.txt
├── requirements.txt            # polymarket-client pinned to 0.11.x
├── requirements-dev.txt        # + pytest
├── .env.example                # copy to .env (git-ignored)
├── tests/                      # tests: markets, METAR, catalog, strategy, execution safety
└── weatherbot/
    ├── catalog.py              # ALL 51 cities → verified ICAO station, unit, timezone, rules phrase
    ├── settings.py             # all creds from os.environ
    ├── models.py               # pydantic records (temps in the station's own unit)
    ├── db.py                   # Supabase: config, stations, market_buckets, audit
    ├── probability.py          # ALL the maths: models + bias persistence + Jev → bucket probabilities
    ├── calibration.py          # per-station model offsets + spread, learned by backtest.py
    ├── strategy.py             # edge = our probability − best ask → pick bucket, FOK price cap
    ├── weather/
    │   ├── metar.py            # aviationweather.gov, every station worldwide in one request
    │   ├── forecast.py         # ~10 models via Open-Meteo (HRRR in US) → consensus + spread
    │   ├── taf.py              # airport forecasts (TAF) for every station, TX max when given
    │   └── afd.py              # NWS Area Forecast Discussion (US offices only)
    ├── ai/
    │   ├── evidence.py         # one bundle per city: obs + every model + TAF + AFD + market
    │   └── jev.py              # Jev via typesafe-sdk: 2 semantic questions per city
    └── exchange/
        ├── markets.py          # Gamma API discovery, bucket parsing, rules verification
        └── polymarket.py       # polymarket-client: order books + FOK-only execution
```

## Safety rails

- **Station mapping** lives in `weatherbot/catalog.py`, checked against every market's rules
  on 2026-09-26. Each day the bot re-reads the rules; if they stop naming the catalogued
  airport, that city is marked not tradeable (`rules_ok = false`) until the catalog is fixed.
- **FOK only**: `exchange/polymarket.py` sends `place_market_order(..., max_price=cap,
  order_type="FOK")`. A test fails the build if `GTC`, `GTD`, `FAK` or `place_limit_order`
  appears anywhere in the bot.
- **Kill switch**: `config.trading_active` is re-read right before each order. When false the
  bot never even creates an authenticated Polymarket client.

## Open TODOs before going live

- `TYPESAFE_API_KEY` in bot/.env for Jev.
- `OPEN_METEO_API_KEY`: commercial key required before live trading.
- Run `python backtest.py` regularly (e.g. weekly) to refresh `calibration/station_calibration.json`.
- Daily budget / loss-limit enforcement across trades.

## How a decision is made (per city, every minute)

1. Code builds a probability for every bucket from ~10 models, each adjusted for how the
   station has been running against that model so far today, floored at the observed high.
2. Jev (TypeSafe System One) answers two questions *in words* — TypeSafe's docs say Jev is
   not a calculator, so it never compares numbers: will the high end up much cooler / cooler /
   as forecast / warmer / much warmer than the models; has the peak already passed? Code
   turns those probabilities into shifts of the distribution. Jev is re-asked only when its
   inputs change.
3. edge = our probability − best ask. Trade the best bucket if edge ≥ min edge AND the models
   alone also say it's cheap. FOK price cap = probability − min edge.
