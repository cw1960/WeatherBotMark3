-- =============================================================================
-- WeatherBotMark3 — migration 003: store the full evidence behind every decision
-- Run once in the Supabase SQL editor after 002. Safe to re-run.
-- =============================================================================

alter table public.trade_audit_logs add column if not exists evidence          jsonb;          -- obs + every model + TAF + AFD + market, exactly as Jev saw it
alter table public.trade_audit_logs add column if not exists model_spread      numeric(5,1);   -- max − min of the models' projected highs
alter table public.trade_audit_logs add column if not exists jev_expected_high numeric(5,1);   -- Jev's own point forecast

comment on column public.trade_audit_logs.evidence is 'Evidence bundle sent to Jev (bot/weatherbot/ai/evidence.py).';
comment on column public.trade_audit_logs.model_high is 'Median of the models'' full-day forecast highs (before observations), station unit. divergence = obs_high_so_far − this.';
comment on column public.trade_audit_logs.secondary_model_high is 'Headline model projected high: HRRR (US) or ECMWF IFS (elsewhere).';
