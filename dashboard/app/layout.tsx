import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "WeatherBot Mark3 — Control Panel",
  description: "Read-only monitor and master switch for the Polymarket weather bot.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <header className="border-b border-[var(--border)] px-6 py-4">
          <div className="mx-auto flex max-w-6xl items-center justify-between">
            <h1 className="text-lg font-semibold tracking-tight">WeatherBot Mark3</h1>
            <span className="text-xs text-[var(--muted)]">METAR × HRRR × Jev · FOK only</span>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
