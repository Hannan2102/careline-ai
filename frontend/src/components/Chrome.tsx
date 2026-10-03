"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const STAFF_LINKS = [
  { href: "/dashboard", label: "Overview" },
  { href: "/calls", label: "Calls" },
  { href: "/trace", label: "Agent Trace" },
  { href: "/patients", label: "Patients" },
  { href: "/appointments", label: "Appointments" },
  { href: "/escalations", label: "Escalations" },
];

/** The clinic mark: a rounded cross inside a soft square. */
export function Logo({ className = "h-9 w-9" }: { className?: string }) {
  return (
    <svg viewBox="0 0 40 40" className={className} aria-hidden="true">
      <rect width="40" height="40" rx="11" fill="var(--color-brand)" />
      <path
        d="M17 10.5h6a1.5 1.5 0 0 1 1.5 1.5v5h5a1.5 1.5 0 0 1 1.5 1.5v3a1.5 1.5 0 0 1-1.5 1.5h-5v5a1.5 1.5 0 0 1-1.5 1.5h-6a1.5 1.5 0 0 1-1.5-1.5v-5h-5A1.5 1.5 0 0 1 9 21.5v-3A1.5 1.5 0 0 1 10.5 17h5v-5a1.5 1.5 0 0 1 1.5-1.5Z"
        fill="#ffffff"
      />
      <circle cx="29.5" cy="10.5" r="3.5" fill="#5fd4c4" />
    </svg>
  );
}

/**
 * The synthetic-data banner.
 *
 * Fixed to the top of every page, on every route, with no way to dismiss it.
 * A screenshot of this site has to be unmistakable at a glance, and a
 * footnote would not survive being cropped.
 */
export function SyntheticBanner() {
  return (
    <div className="border-b border-amber-200 bg-amber-50 text-amber-900">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-center gap-x-3 gap-y-1 px-4 py-1.5 text-xs">
        <span className="rounded-full bg-amber-600 px-2 py-0.5 text-[10px] font-semibold tracking-wider text-white">
          DEMO · SYNTHETIC DATA
        </span>
        <span>
          Fictional clinic. Every patient, appointment, and prescription is invented.
        </span>
        <span className="opacity-75">Not a medical device · Not for clinical use</span>
      </div>
    </div>
  );
}

export function NavBar() {
  const pathname = usePathname();
  const onCall = pathname === "/";
  return (
    <header className="sticky top-0 z-20 border-b border-edge bg-white/85 backdrop-blur-md">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-3 px-4 py-3">
        <Link href="/" className="flex items-center gap-3">
          <Logo />
          <span className="leading-tight">
            <span className="block text-[15px] font-semibold tracking-tight text-brand">
              CareLine
            </span>
            <span className="block text-xs text-muted">Oakwood Family Medicine</span>
          </span>
        </Link>

        <nav className="flex flex-wrap items-center gap-1 text-sm">
          {STAFF_LINKS.map((link) => {
            const active = pathname.startsWith(link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                className={`rounded-full px-3 py-1.5 transition-colors ${
                  active
                    ? "bg-accent-soft font-medium text-accent"
                    : "text-muted hover:bg-ink hover:text-strong"
                }`}
              >
                {link.label}
              </Link>
            );
          })}
        </nav>

        <Link
          href="/"
          className={`ml-auto inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-semibold shadow-sm transition-colors ${
            onCall
              ? "bg-accent-soft text-accent"
              : "bg-accent text-white hover:bg-accent/90"
          }`}
        >
          <PhoneIcon className="h-4 w-4" />
          Talk to CareLine
        </Link>
      </div>
    </header>
  );
}

export function PhoneIcon({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={className} aria-hidden="true">
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M5 4h3l2 5-2.5 1.5a11 11 0 0 0 6 6L15 14l5 2v3a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2Z"
      />
    </svg>
  );
}

export function Footer() {
  return (
    <footer className="border-t border-edge bg-white">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-6 text-xs text-muted">
        <span className="flex items-center gap-2">
          <Logo className="h-5 w-5" /> CareLine AI — a portfolio demonstration of an agentic
          patient-access line.
        </span>
        <span>
          It never gives medical advice and never authorises a refill — requests go to a
          clinician.
        </span>
      </div>
    </footer>
  );
}
