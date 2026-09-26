# /bot — execution daemon

```
bot/
├── main.py                     # asyncio daemon: discover → observe → evaluate → gate → FOK → audit
├── discover_markets.py         # report: every city's markets today/tomorrow + live METAR bucket
├── edge_report.py              # report: every model's forecast per city, disagreement, TAF
├── requirements.txt            # polymarket-client pinned to 0.11.x
├── requirements-dev.txt        # + pytest
├── .env.example                # copy to .env (git-ignored)
├── tests/                      # tests: markets, METAR, catalog, strategy, execution safety
└── weatherbot/
    ├── catalog.py              # ALL 51 cities → verified ICAO station, unit, timezone, rules phrase
    ├── settings.py             # all creds from os.environ
    ├── models.py               # pydantic records (temps in the station's own unit)
    ├── db.py                   # Supabase: config, stations, market_buckets, audit
    ├── strategy.py             # divergence + Jev gate → TradeSignal | skip
    ├── weather/
    │   ├── metar.py            # aviationweather.gov, every station worldwide in one request
    │   ├── forecast.py         # ~10 models via Open-Meteo (HRRR in US) → consensus + spread
    │   ├── taf.py              # airport forecasts (TAF) for every station, TX max when given
    │   └── afd.py              # NWS Area Forecast Discussion (US offices only)
    ├── ai/
    │   ├── evidence.py         # one bundle per city: obs + every model + TAF + AFD + market
    │   └── jev.py              # Jev System One reads the bundle — every city
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

- `ai/jev.py`: confirm Jev's real API contract (currently assumes OpenAI-compatible chat).
- `OPEN_METEO_API_KEY`: commercial key required before live trading.
- `strategy.py`: replace the placeholder probability with a real model (use the model spread
  and Jev's expected_high to build a distribution over buckets).
- Daily budget / loss-limit enforcement across trades.
