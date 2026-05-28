# Deploying Revven.V2 on Vercel

Revven.V2 is a Flask app deployed as a single Python serverless function on Vercel. It is part of the HavenOS ecosystem and **must point at the Haven Supabase project** via environment variables — there is no separate database for this surface.

## 1. Prerequisites

- A Vercel account with access to the Haven org/team.
- Access to the Haven Supabase project (for `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`).
- API keys / credentials for: Groq, PriceLabs, Booking.com Connectivity, Hostaway. See `.env.example` for the full list.

## 2. Import the repo

Vercel Dashboard → **Add New… → Project → Import Git Repository** → select `zoppinator123/Revven.V2`.

Configure:

| Field            | Value                                       |
| ---------------- | ------------------------------------------- |
| Framework Preset | **Other**                                   |
| Root Directory   | `./`                                        |
| Build Command    | *(leave empty)*                             |
| Output Directory | *(leave empty)*                             |
| Install Command  | *(leave empty — picks up `requirements.txt`)* |

`vercel.json` already declares the build:

```json
{
  "builds": [
    { "src": "api/index.py", "use": "@vercel/python",
      "config": { "maxLambdaSize": "250mb", "includeFiles": "Haven/**" } }
  ],
  "routes": [{ "src": "/(.*)", "dest": "api/index.py" }]
}
```

## 3. Environment variables

Add all variables from `.env.example` under **Project Settings → Environment Variables** (Production, Preview, Development as appropriate).

**HavenOS / Supabase — must use the Haven project, not a one-off:**

| Variable                    | Scope                 | Notes                                                |
| --------------------------- | --------------------- | ---------------------------------------------------- |
| `SUPABASE_URL`              | All                   | Haven Supabase project URL                           |
| `SUPABASE_ANON_KEY`         | All                   | Safe for SSR; do not expose to untrusted browsers    |
| `SUPABASE_SERVICE_ROLE_KEY` | Production / Preview  | **Server-only.** Bypasses RLS — never expose to UI   |

**Integration keys:** `GROQ_API_KEY`, `PRICELABS_API_KEY`, `PRICELABS_EMAIL`, `PRICELABS_PASSWORD`, `PRICELABS_POST_APPLY_ENDPOINTS` (optional), `BOOKING_CLIENT_ID`, `BOOKING_CLIENT_SECRET`, `HOSTAWAY_API_TOKEN`, `WHEELHOUSE_API_KEY` (optional).

## 4. Deploy

- **CLI:** `npm i -g vercel && vercel --prod` from the repo root.
- **Dashboard:** click **Deploy** on the imported project.

Vercel will install `requirements.txt`, build `api/index.py` with `@vercel/python`, and route every request to the Flask `app`.

## 5. Verify

After the deploy finishes:

- Hit the production URL — the dashboard index should render.
- Confirm in Vercel logs that the Flask app booted without `KeyError` on env vars.
- Check that Groq-streamed analyses succeed (requires `GROQ_API_KEY`).

## Known limitations & gotchas

- **Cold starts.** Bundling ~48 MB of pre-computed CSV/JSON snapshots into the function means cold starts will be slower than typical. Acceptable for an internal ops dashboard.
- **Read-only filesystem.** Vercel serverless functions can only write to `/tmp`. Anywhere the app currently writes JSON back to disk (e.g. `pricelabs_weekly_action_queue.json`, `booking_promotion_lab.json`) is **ephemeral**. Persistent action state needs to be migrated to the Haven Supabase project before this dashboard is relied on in production.
- **No Playwright on Vercel.** `Haven/requirements.txt` lists `playwright` for the local PriceLabs browser-export sync; the root `requirements.txt` used by Vercel intentionally excludes it. Run browser-based sync locally only.
- **Long-running streams.** The dashboard uses Server-Sent Events for Groq analyses. Vercel's default Hobby plan caps function execution at 10 s; use Pro for the 60–300 s budgets the longer reports need.
- **Function size.** The included data files put the function near (but under) Vercel's 250 MB unzipped limit. If new large snapshots are added, move them to Supabase Storage or a separate bucket instead of bundling.

## Security checklist

- [ ] No `.env` file is committed (only `.env.example`).
- [ ] `SUPABASE_SERVICE_ROLE_KEY` is set only on the server, never exposed to the browser.
- [ ] Any secret previously pasted into chat / email / a public PR has been rotated.
- [ ] Production env vars are scoped to **Production** only where appropriate.
