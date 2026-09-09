import type { NextConfig } from "next";

const config: NextConfig = {
  // The dashboard is a client of the FastAPI backend, not a proxy for it:
  // every fetch goes straight to NEXT_PUBLIC_API_BASE_URL from the browser,
  // which keeps `next build` hermetic and makes a backend that is down show
  // up as a visible connection error rather than a build failure.
  reactStrictMode: true,
};

export default config;
