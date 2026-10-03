import type { Metadata } from "next";
import { Inter } from "next/font/google";
import { Footer, NavBar, SyntheticBanner } from "@/components/Chrome";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });

export const metadata: Metadata = {
  title: "CareLine AI — Oakwood Family Medicine",
  description:
    "Talk to CareLine, an AI patient-access line, and see every turn it takes. Synthetic data only.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={inter.variable}>
      <body className="flex min-h-screen flex-col font-sans antialiased">
        <SyntheticBanner />
        <NavBar />
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-8">{children}</main>
        <Footer />
      </body>
    </html>
  );
}
