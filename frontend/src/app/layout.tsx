import type { Metadata } from "next";
import { NavBar, SyntheticBanner } from "@/components/Chrome";
import "./globals.css";

export const metadata: Metadata = {
  title: "CareLine AI — admin dashboard",
  description:
    "Operations view for the CareLine AI patient-access agent. Synthetic data only.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <SyntheticBanner />
        <NavBar />
        <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
        <footer className="mx-auto max-w-7xl px-4 pb-10 text-xs text-muted">
          CareLine AI is a portfolio demonstration. It does not give medical advice, and it
          never authorises a prescription refill — a request is queued for a clinician.
        </footer>
      </body>
    </html>
  );
}
