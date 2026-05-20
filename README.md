# AI Brand Scale Platform

Multi-tenant AI ad platform — clients, products, animated ads, advertorials, finance, static creatives.

## Quick Local Run

```bash
cp .env.example .env
# Fill KIE_AI_API_KEY (get from https://kie.ai/api-key)
pip install -r requirements.txt
python3 execution/brand_scraper_server.py
# → http://localhost:8766
```

## Deploy

See `DEPLOY.md` for Render / Railway / Fly.io step-by-step.

## Architecture

- **One Python server** (`execution/brand_scraper_server.py`) — frontend + REST API + ad templates
- **ThreadingHTTPServer** for concurrent users
- **Per-user isolation** via HMAC tokens
- **Filesystem state** in `.tmp/` (mount as volume in production)
- **KIE.AI integration** — Nano Banana Pro, Kling 2.6, Gemini, ElevenLabs

## Features

- Brand profiles with AI auto-scrape (Gemini 2.5 Pro)
- Products with multi-image uploads
- Animated Ads (PixarADS) — keyframes → Kling video → ElevenLabs voiceover
- Advertorials — 10 angles → Shopify-style HTML with inline images
- Static Creatives — 399 ad templates + Nano Banana generation
- Finance — contracts, invoices, subscriptions
- Multi-user auth (signup/login, HMAC sessions)
