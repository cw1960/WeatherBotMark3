/** Row types mirroring supabase/schema.sql. Regenerate with `supabase gen types` later. */

export type Station = {
  id: string;
  city_slug: string;
  city_label: string;
  icao_code: string;
  airport_name: string | null;
  rules_phrase: string | null;
  nws_office: string | null;
  timezone: string;
  country: string | null;
  resolution_source: "noaa" | "wunderground" | "hko";
  unit: "F" | "C";
  is_active: boolean;
  updated_at: string;
};

export type Config = {
  id: 1;
  trading_active: boolean;
  max_budget_usdc: number;
  max_order_usdc: number;
  daily_loss_limit_usdc: number;
  min_divergence_deg: number;
  min_edge_pct: number;
  min_jev_confidence: number;
  alert_divergence_deg: number;
  alert_drawdown_pct: number;
  poll_interval_seconds: number;
  updated_by: string | null;
  updated_at: string;
};

export type Decision = "skip" | "simulated" | "live";

export type TradeAuditLog = {
  id: number;
  created_at: string;
  observed_at: string | null;
  city_slug: string | null;
  icao_code: string;
  market_id: string;
  condition_id: string;
  token_id: string | null;
  target_date: string | null;
  unit: "F" | "C" | null;
  model_name: string | null;
  obs_temp: number | null;
  obs_high_so_far: number | null;
  model_high: number | null;
  divergence: number | null;
  market_outcome: string | null;
  market_price: number | null;
  edge_pct: number | null;
  jev_sentiment: Record<string, unknown> | null;
  jev_confidence: number | null;
  jev_direction: "warmer" | "cooler" | "neutral" | "uncertain" | null;
  decision: Decision;
  skip_reason: string | null;
  trading_active: boolean;
  order_side: "BUY" | "SELL" | null;
  order_type: "FOK";
  order_price: number | null;
  order_size_usdc: number | null;
  order_status: string | null;
  fill_size: number | null;
  fill_price: number | null;
  error_message: string | null;
  cluster_id: string | null;
};
