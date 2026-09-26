"use client";

import { createBrowserClient } from "@supabase/ssr";

/** Client Components: realtime subscriptions and the trading_active toggle. */
export function createClient() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
  );
}
