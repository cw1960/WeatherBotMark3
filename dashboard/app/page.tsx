import { createClient } from "@/lib/supabase/server";
import type { Config, Station, TradeAuditLog } from "@/lib/types";
import { TradingSwitch } from "@/components/TradingSwitch";
import { AuditTable } from "@/components/AuditTable";

export const dynamic = "force-dynamic";

export default async function Dashboard() {
  const supabase = await createClient();

  const [{ data: config }, { data: stations }, { data: logs }] = await Promise.all([
    supabase.from("config").select("*").eq("id", 1).single<Config>(),
    supabase.from("stations").select("*").order("city_label").returns<Station[]>(),
    supabase
      .from("trade_audit_logs")
      .select("*")
      .order("created_at", { ascending: false })
      .limit(100)
      .returns<TradeAuditLog[]>(),
  ]);

  return (
    <div className="space-y-8">
      <section className="grid gap-4 md:grid-cols-3">
        <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4 md:col-span-1">
          <h2 className="mb-3 text-sm font-medium text-[var(--muted)]">Master switch</h2>
          <TradingSwitch initial={config?.trading_active ?? false} />
          <p className="mt-3 text-xs text-[var(--muted)]">
            Off → bot logs simulated trades only. On → live FOK orders within budget.
          </p>
        </div>

        <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
          <h2 className="mb-3 text-sm font-medium text-[var(--muted)]">Risk limits</h2>
          <dl className="space-y-1 text-sm">
            <Row k="Max budget" v={`$${config?.max_budget_usdc ?? "—"}`} />
            <Row k="Max per order" v={`$${config?.max_order_usdc ?? "—"}`} />
            <Row k="Daily loss limit" v={`$${config?.daily_loss_limit_usdc ?? "—"}`} />
            <Row k="Min divergence" v={`${config?.min_divergence_deg ?? "—"}°F`} />
            <Row k="Min Jev confidence" v={`${config?.min_jev_confidence ?? "—"}`} />
          </dl>
        </div>

        <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
          <h2 className="mb-3 text-sm font-medium text-[var(--muted)]">Stations</h2>
          <ul className="space-y-1 text-sm">
            {(stations ?? []).map((s) => (
              <li key={s.id} className="flex justify-between">
                <span>{s.city_label}</span>
                <span className="font-mono text-[var(--muted)]">
                  {s.icao_code}
                  {!s.is_active && " · off"}
                </span>
              </li>
            ))}
            {!stations?.length && <li className="text-[var(--muted)]">No stations yet.</li>}
          </ul>
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-medium text-[var(--muted)]">Audit log (latest 100)</h2>
        <AuditTable initial={logs ?? []} />
      </section>
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between">
      <dt className="text-[var(--muted)]">{k}</dt>
      <dd className="font-mono">{v}</dd>
    </div>
  );
}
