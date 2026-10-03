# Deploying to Render

Everything runs on Render from `render.yaml`. One Blueprint, three resources:

| Resource | What it is | Free tier |
|---|---|---|
| `careline-api` | FastAPI agent + voice WebSocket (Docker). The only place API keys exist. | Yes |
| `careline-web` | Next.js site: the call page and the staff dashboard. | Yes |
| `careline-db` | Postgres: calls, turns, audit, escalations, metered spend. | Yes, 30 days |

## Where the keys live

`GROQ_API_KEY` and `DEEPGRAM_API_KEY` are `sync: false` in `render.yaml`. Render asks
for them once, when the Blueprint is created, and stores them encrypted on the
`careline-api` service. Specifically:

- They are not in the repository, which ignores `.env`.
- They are not in the site. The browser only ever receives the API's public URL.
- They are not in any API response or log. Logging redacts credentials, and
  `/api/system/status` reports provider *names* only.

## First deploy

1. Push the branch to GitHub.
2. In Render, go to **New → Blueprint** and pick the `careline-ai` repository and
   branch. Render reads `render.yaml`.
3. Render prompts for the two secrets, `GROQ_API_KEY` and `DEEPGRAM_API_KEY`.
   The public URLs are plain values in `render.yaml`: the site is
   `https://careline-web.onrender.com` and the API
   `https://careline-api-dk7v.onrender.com` (Render added the suffix because the
   name was taken). A fresh Blueprint elsewhere will get different URLs; change
   `DASHBOARD_ORIGINS`, `PUBLIC_BASE_URL` and `NEXT_PUBLIC_API_BASE_URL` to match.

4. Apply. The database comes up first, then the API (which creates its tables on
   start), then the site.
5. Open `https://careline-web.onrender.com` and press call.

Changing a URL is a commit to `render.yaml`: the Blueprint syncs the value and the
site is rebuilt, which matters because `NEXT_PUBLIC_API_BASE_URL` is compiled in.

## What the free tier means for a demo

- **Services sleep after 15 minutes idle.** The first visit after a quiet spell waits
  roughly a minute while the API wakes up, and a call started during that minute fails
  to connect. Open the site, wait for the dashboard to load (that wakes the API), then
  call. The paid Starter plan does not sleep.
- **The free database expires after 30 days.** Upgrade it, or recreate it, before then.
- **The synthetic EHR is in memory.** Patients and appointments reset whenever the API
  restarts or sleeps. Calls, traces and escalations do not, because they are in
  Postgres.

## Spending

The budget guard runs on the server: $20 project ceiling, warning at $15, $1 per call.
Spend is written to Postgres and carried forward at every start, so the ceiling holds
across restarts and redeploys. The site is public to anyone with the link, so the
ceiling is what protects the keys' credit. Lower `MAX_ESTIMATED_PROJECT_COST_USD` on
`careline-api` to tighten it.

## Twilio (next phase)

The phone line uses the same `careline-api` service: Twilio's webhook and media stream
point at `PUBLIC_BASE_URL`. Nothing else needs hosting. The service should be on a
plan that does not sleep before it takes real calls, because a caller cannot wait a
minute for it to wake.
