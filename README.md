# Revven.V2

Revven.V2 is the **HavenOS-connected dashboard and workflow layer for Haven Vacation Rentals**. It is the operator-facing surface for revenue management, listing quality, and channel-promotion workflows across the Haven STR portfolio, and it reads/writes shared state through the Haven HavenOS Supabase project.

## Where this fits in HavenOS

HavenOS is the shared operational backbone for Haven Vacation Rentals. Revven.V2 plugs into it as:

- **A dashboard / workflow layer** — Flask UI that surfaces PriceLabs, Booking.com, Hostaway, and listing-quality data for the Haven portfolio.
- **A HavenOS client, not a source of truth** — persistent state (portfolio metadata, applied-action history, listing IDs) is expected to live in the Haven Supabase project. Revven.V2 connects to it via environment variables; it never ships keys in the repo. Revven-owned tables are isolated in a `revven` schema (`pricing_actions`, `booking_promotions`, `pricelabs_snapshots`, `healthz`) and CSV uploads target the `revven-uploads` Storage bucket — no HavenOS public tables are touched. When `SUPABASE_SERVICE_ROLE_KEY` is unset or a call fails, the app falls back to the bundled JSON/CSV snapshots.
- **One of several Haven surfaces** — other HavenOS surfaces (guest-facing, ops, etc.) read from and write to the same Supabase project, so changes here must respect the shared schema.

## What it does

- PriceLabs revenue recommendations: review, approve, apply, and verify pricing changes.
- Weekly action queue generated from the latest PriceLabs portfolio export.
- Booking.com Promo Lab: draft / AI-review / approve / push promotions via the Connectivity Promotions API.
- Listing quality + Airbnb/VRBO review status surfaced alongside pricing.

## Repository layout

```
.
├── Haven/                  # Flask app + data files (the actual dashboard)
│   ├── app.py              # Flask entry point
│   ├── templates/          # UI templates
│   ├── requirements.txt    # Local-dev Python deps (includes Playwright)
│   └── ...                 # PriceLabs / Booking / Hostaway / Wheelhouse helpers
├── api/index.py            # Vercel serverless entrypoint (re-exports Flask `app`)
├── vercel.json             # Vercel build / route config
├── requirements.txt        # Vercel runtime deps (Playwright excluded)
├── .env.example            # All env vars required by the app
└── app.py                  # Local convenience launcher
```

## Run locally

```bash
cp .env.example .env       # then fill in real values — do NOT commit .env
cd Haven
python3 -m pip install -r requirements.txt
python3 app.py             # serves http://localhost:8080
```

Or from the repo root: `python3 app.py` (delegates to `Haven/app.py`).

## Deploy on Vercel

Revven.V2 deploys to Vercel as a single Python serverless function that wraps the Flask app.

**Project settings**

| Setting             | Value                                                            |
| ------------------- | ---------------------------------------------------------------- |
| Framework Preset    | Other                                                            |
| Build Command       | *(leave empty — handled by `vercel.json`)*                       |
| Output Directory    | *(leave empty)*                                                  |
| Install Command     | *(leave empty — Vercel installs from `requirements.txt`)*        |
| Root Directory      | `./` (repo root)                                                 |

`vercel.json` builds `api/index.py` with `@vercel/python` and routes all traffic to it. The `includeFiles: "Haven/**"` directive bundles the data files (CSV / JSON snapshots) the Flask app reads at startup.

**Deploy via CLI**

```bash
npm i -g vercel
vercel              # first run links the project and prompts for env vars
vercel --prod       # production deploy
```

### Required environment variables (set in Vercel → Project Settings → Environment Variables)

**HavenOS / Supabase (Haven Vacation Rentals shared backend)** — use the Haven Supabase project, not a one-off project:

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY` *(server-only; never expose client-side)*

**Integrations**

- `Grok_XAI_API_KEY` *(preferred; falls back to `GROQ_API_KEY` if unset)*
- `PRICELABS_API_KEY`, `PRICELABS_EMAIL`, `PRICELABS_PASSWORD`, optional `PRICELABS_POST_APPLY_ENDPOINTS`
- `BOOKING_CLIENT_ID`, `BOOKING_CLIENT_SECRET`
- `HOSTAWAY_API_TOKEN`
- `WHEELHOUSE_API_KEY` *(optional / legacy)*

See [`.env.example`](./.env.example) for the full list and [`DEPLOY.md`](./DEPLOY.md) for step-by-step instructions and known limitations.

## Security

- **Never commit secrets.** `.env` and friends are git-ignored; only `.env.example` (placeholders) is tracked.
- The Supabase **service-role** key has full database access. Store it only in Vercel server-side environment variables, never in any file shipped to the browser.
- Rotate any key that has been pasted into chat, email, or a public commit.

## License

Internal — Haven Vacation Rentals.
