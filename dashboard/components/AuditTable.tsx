"use client";

import { useEffect, useState } from "react";
import { createClient } from "@/lib/supabase/client";
import type { TradeAuditLog } from "@/lib/types";

const decisionColor: Record<TradeAuditLog["decision"], string> = {
  live: "var(--live)",
  simulated: "var(--sim)",
  skip: "var(--skip)",
};

export function AuditTable({ initial }: { initial: TradeAuditLog[] }) {
  const [rows, setRows] = useState(initial);

  useEffect(() => {
    const supabase = createClient();
    const channel = supabase
      .channel("audit-feed")
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "trade_audit_logs" },
        (payload) => setRows((prev) => [payload.new as TradeAuditLog, ...prev].slice(0, 100)),
      )
      .subscribe();
    return () => {
      supabase.removeChannel(channel);
    };
  }, []);

  if (!rows.length) {
    return (
      <p className="rounded-lg border border-[var(--border)] p-6 text-sm text-[var(--muted)]">
        No audit rows yet — start the bot and they will stream in here.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
      <table className="w-full text-left text-xs">
        <thead className="bg-[var(--card)] text-[var(--muted)]">
          <tr>
            <Th>Time (UTC)</Th>
            <Th>City</Th>
            <Th>Obs high</Th>
            <Th>Model high</Th>
            <Th>Δ</Th>
            <Th>Jev</Th>
            <Th>Outcome</Th>
            <Th>Price</Th>
            <Th>Decision</Th>
            <Th>Order</Th>
            <Th>Note</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-t border-[var(--border)]">
              <Td mono>{new Date(r.created_at).toISOString().slice(5, 19).replace("T", " ")}</Td>
              <Td mono>
                {r.city_slug ?? r.icao_code}
                <span className="text-[var(--muted)]"> {r.icao_code}</span>
              </Td>
              <Td mono>{deg(r.obs_high_so_far, r.unit)}</Td>
              <Td mono>
                {deg(r.model_high, r.unit)}
                {r.model_name && <span className="text-[var(--muted)]"> {r.model_name}</span>}
              </Td>
              <Td mono>{r.divergence == null ? "—" : (r.divergence > 0 ? "+" : "") + r.divergence}</Td>
              <Td>
                {r.jev_direction ?? "—"}
                {r.jev_confidence != null && (
                  <span className="text-[var(--muted)]"> {Math.round(r.jev_confidence * 100)}%</span>
                )}
              </Td>
              <Td>{r.market_outcome ?? "—"}</Td>
              <Td mono>{fmt(r.market_price, 2)}</Td>
              <Td>
                <span
                  className="rounded px-1.5 py-0.5 font-semibold uppercase"
                  style={{ color: decisionColor[r.decision] }}
                >
                  {r.decision}
                </span>
              </Td>
              <Td mono>
                {r.order_status ?? "—"}
                {r.order_size_usdc != null && ` $${r.order_size_usdc}`}
              </Td>
              <Td className="max-w-[24rem] truncate text-[var(--muted)]">
                {r.error_message ?? r.skip_reason ?? ""}
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function deg(n: number | null, unit: TradeAuditLog["unit"]) {
  return n == null ? "—" : `${n.toFixed(1)}°${unit ?? ""}`;
}
function fmt(n: number | null, d = 1) {
  return n == null ? "—" : n.toFixed(d);
}
function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-3 py-2 font-medium">{children}</th>;
}
function Td({
  children,
  mono,
  className = "",
}: {
  children: React.ReactNode;
  mono?: boolean;
  className?: string;
}) {
  return <td className={`px-3 py-2 ${mono ? "font-mono" : ""} ${className}`}>{children}</td>;
}
