"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { API_BASE } from "@/lib/api";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/calls", label: "Calls" },
  { href: "/trace", label: "Agent Trace" },
  { href: "/patients", label: "Patients" },
  { href: "/appointments", label: "Appointments" },
  { href: "/escalations", label: "Escalations" },
];

/**
 * The synthetic-data banner.
 *
 * Fixed to the top of every page, on every route, with no way to dismiss it.
 * A demo screenshot of this dashboard has to be unmistakable at a glance, and
 * a footnote would not survive being cropped.
 */
export function SyntheticBanner() {
  return (
    <div className="bg-warn text-black">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-3 gap-y-1 px-4 py-1.5 text-xs font-semibold">
        <span className="rounded bg-black/80 px-1.5 py-0.5 font-mono text-warn">
          SYNTHETIC DATA
        </span>
        <span>
          Fictional clinic. Every patient, appointment, and prescription here is invented.
        </span>
        <span className="opacity-80">Not a medical device · Not HIPAA compliant · Not for clinical use</span>
      </div>
    </div>
  );
}

export function NavBar() {
  const pathname = usePathname();
  return (
    <header className="sticky top-0 z-10 border-b border-edge bg-ink/95 backdrop-blur">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
        <Link href="/" className="flex items-baseline gap-2">
          <span className="text-base font-semibold tracking-tight">CareLine AI</span>
          <span className="text-xs text-muted">Oakwood Family Medicine</span>
        </Link>
        <nav className="flex flex-wrap gap-1 text-sm">
          {LINKS.map((link) => {
            const active =
              link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                className={`rounded px-2.5 py-1 ${
                  active ? "bg-edge text-white" : "text-muted hover:bg-edge/50 hover:text-white"
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>
        <span className="ml-auto font-mono text-[11px] text-muted">{API_BASE}</span>
      </div>
    </header>
  );
}
