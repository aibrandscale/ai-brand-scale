# AI Brand Scale — Deploy Guide

Целта: други хора да могат да ползват приложението чрез публичен URL.

## Архитектура
- **Един Python сървър** (`execution/brand_scraper_server.py`) обслужва:
  - Frontend (`layout_preview.html`)
  - REST API (auth, scrape, pixar, advertorial, finance, statics)
  - Шаблони (`ad_templates/`)
- **ThreadingHTTPServer** — concurrent users работят паралелно
- **Filesystem state** — `.tmp/users.json` + `.tmp/<feature>_jobs/`
- **Per-user изолация** чрез auth токени (HMAC-SHA256, 30 дни валидност)

## Препоръчани хостове

| Хост | Цена | Време | Setup |
|---|---|---|---|
| **Render** | $0 (free, sleeps) или $7/мес | 5 мин | git push + connect |
| **Railway** | $5/мес (with $5 free credit) | 5 мин | git push + auto |
| **Fly.io** | $0 free tier (24/7) | 10 мин | CLI deploy |

---

## 🟢 Опция 1 — Render (НАЙ-ЛЕСНО)

### Стъпки:

1. **GitHub репо:**
   ```bash
   cd "Meta Ads Launcher - OPC Package 2"
   git init
   git add .
   git commit -m "Initial AI Brand Scale"
   ```

   Отиди на github.com → нов repo `ai-brand-scale` → push:
   ```bash
   git remote add origin https://github.com/USERNAME/ai-brand-scale.git
   git branch -M main
   git push -u origin main
   ```

2. **Render setup:**
   - Отиди на https://render.com → New +
   - Web Service → Connect GitHub → избери `ai-brand-scale`
   - Render авто-открива `render.yaml` и попълва всичко
   - В Environment Variables → добави **`KIE_AI_API_KEY`** (от kie.ai)
   - Create Web Service

3. След 3-5 мин build → имаш URL: `https://ai-brand-scale.onrender.com`

### Free vs Starter
- **Free** = заспива след 15 мин неактивност, cold start ~30s
- **Starter** ($7/мес) = винаги онлайн, нужен за реални клиенти

---

## 🟣 Опция 2 — Railway

1. https://railway.app → New Project → Deploy from GitHub
2. Свържи `ai-brand-scale` репо
3. Variables → `KIE_AI_API_KEY`
4. Settings → Generate Domain → имаш URL `https://ai-brand-scale.up.railway.app`

Auto-открива `Procfile` и `requirements.txt`.

---

## 🟠 Опция 3 — Fly.io

```bash
# Install CLI
curl -L https://fly.io/install.sh | sh

# Login
fly auth signup

# Launch (auto-открива fly.toml + Dockerfile)
fly launch --no-deploy

# Set secrets
fly secrets set KIE_AI_API_KEY=YOUR_KEY
fly secrets set SESSION_SECRET=$(openssl rand -hex 32)

# Create persistent volume for user data
fly volumes create data --size 5 --region fra

# Deploy
fly deploy
```

URL: `https://ai-brand-scale.fly.dev`

---

## След deploy

1. Отвори URL → ще покаже login overlay
2. Кликни Регистрация → създай админ профил
3. Сподели URL с другите хора → всеки прави свой signup
4. Всеки user има изолирана история на jobs

## Production checklist

- [ ] Set strong `SESSION_SECRET` (не дефолтния)
- [ ] Set `KIE_AI_API_KEY` от kie.ai (зареди $10+ credits)
- [ ] Persistent volume mounted на `.tmp/` (auto в render.yaml/fly.toml)
- [ ] HTTPS forced (всичките 3 хоста го правят автоматично)
- [ ] Backup `.tmp/users.json` периодично

## Локално с пълна функционалност

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Попълни KIE_AI_API_KEY
python3 execution/brand_scraper_server.py
# http://localhost:8766
```
