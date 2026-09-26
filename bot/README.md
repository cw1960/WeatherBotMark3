# /bot — execution daemon

```
bot/
├── main.py                     # asyncio daemon: poll → evaluate → gate → FOK → audit
├── requirements.txt
├── pyproject.toml
├── .env.example                # copy to .env (git-ignored)
├── tests/test_strategy.py
└── weatherbot/
    ├── settings.py             # all creds from os.environ
    ├── models.py               # pydantic records mirroring the Supabase schema
    ├── db.py                   # Supabase (service_role) — config, stations, audit
    ├── strategy.py             # divergence + Jev gate → TradeSignal | skip
    ├── weather/
    │   ├── metar.py            # NWS observations by exact ICAO
    │   ├── hrrr.py             # HRRR forecast high (TODO: real source)
    │   └── afd.py              # Area Forecast Discussion text by WFO
    ├── ai/jev.py               # Jev System One via httpx
    └── exchange/polymarket.py  # polymarket-client AsyncSecureClient, FOK only
```

## Run locally

```bash
cd bot
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in
pytest                 # strategy smoke tests
python main.py
```

## Deploy (US-East Linux VPS)

Run as a systemd service with `EnvironmentFile=/opt/weatherbot/bot/.env` and
`Restart=always`. Keep `config.trading_active = false` until simulated audit rows
look right in the dashboard.

## Open TODOs before going live

- `exchange/polymarket.py`: confirm `AsyncSecureClient` constructor / `create_order` / `post_order` names against the installed `polymarket-client` version.
- `weather/hrrr.py`: pick and implement the HRRR source (NOMADS grib_filter or Open-Meteo).
- `main.py`: map `metar.high_so_far_f` → the correct outcome bucket and CLOB `token_id` via the Gamma API for each market.
- `ai/jev.py`: verify Jev's endpoint path and JSON response shape.
