-- 004: the bot now uses the BLEND RULE ~60 min before each city's typical peak.
-- Our view is blended with the market's own (mid) prices, and a trade needs the blend to beat
-- the best ask by at least min_edge_pct. The intraday backtest used 4% for this rule.
-- Safe to run more than once.
alter table public.config alter column min_edge_pct set default 4.00;
update public.config set min_edge_pct = 4.00 where min_edge_pct = 8.00;
