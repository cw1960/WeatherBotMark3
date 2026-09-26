"use client";

import { useEffect, useState, useTransition } from "react";
import { createClient } from "@/lib/supabase/client";

/**
 * The only write the dashboard performs: flipping config.trading_active.
 * Turning it ON requires a typed confirmation to avoid accidental live trading.
 */
export function TradingSwitch({ initial }: { initial: boolean }) {
  const [active, setActive] = useState(initial);
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);

  // Stay in sync if the row changes elsewhere (another tab, SQL editor).
  useEffect(() => {
    const supabase = createClient();
    const channel = supabase
      .channel("config-switch")
      .on(
        "postgres_changes",
        { event: "UPDATE", schema: "public", table: "config", filter: "id=eq.1" },
        (payload) => setActive(Boolean((payload.new as { trading_active: boolean }).trading_active)),
      )
      .subscribe();
    return () => {
      supabase.removeChannel(channel);
    };
  }, []);

  function toggle() {
    const next = !active;
    if (next) {
      const typed = window.prompt('Type "LIVE" to enable real FOK orders:');
      if (typed !== "LIVE") return;
    }
    start(async () => {
      setError(null);
      const supabase = createClient();
      const { data: userData } = await supabase.auth.getUser();
      const { error } = await supabase
        .from("config")
        .update({ trading_active: next, updated_by: userData.user?.email ?? null })
        .eq("id", 1);
      if (error) setError(error.message);
      else setActive(next);
    });
  }

  return (
    <div>
      <button
        onClick={toggle}
        disabled={pending}
        aria-pressed={active}
        className={`w-full rounded-md px-4 py-3 text-sm font-semibold transition ${
          active
            ? "bg-[var(--live)] text-black hover:brightness-110"
            : "bg-[var(--border)] text-[var(--fg)] hover:brightness-125"
        } disabled:opacity-50`}
      >
        {pending ? "Saving…" : active ? "TRADING LIVE — click to stop" : "Trading OFF — click to enable"}
      </button>
      {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
    </div>
  );
}
