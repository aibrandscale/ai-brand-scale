# Supabase Setup — AI Brand Scale

Стъпки за връзване на приложението към Postgres база данни (Supabase).

---

## 1. Създай Supabase проект (3 мин)

1. Отиди на https://supabase.com → **Start your project** → sign in с GitHub
2. **New Project**:
   - Name: `ai-brand-scale`
   - Database password: **генерирай силна парола** (запази я някъде безопасно)
   - Region: **Frankfurt (eu-central-1)** — най-близо до България
   - Plan: **Free**
3. Изчакай ~2 мин докато БД-то се initialize-не

---

## 2. Изпълни схемата (1 мин)

1. В Supabase dashboard → ляво меню → **SQL Editor**
2. Натисни **New query**
3. Копирай съдържанието на `db/schema.sql` (от това репо) и paste-ни
4. **Run** (или Ctrl/Cmd+Enter)
5. Трябва да видиш `Success. No rows returned`

Провери: ляво меню → **Table Editor** → трябва да видиш `users` и `job_history` таблици.

---

## 3. Вземи connection string (1 мин)

1. Ляво меню → **Project Settings** (⚙️ долу вляво) → **Database**
2. Скролни до **Connection string** секцията
3. Избери таб **URI**
4. Избери **Mode: Session** (НЕ Transaction — нашият connection pool ползва session mode)
5. Копирай URL-а. Изглежда така:
   ```
   postgresql://postgres.xxxxxxxxxxxxxxxx:[YOUR-PASSWORD]@aws-0-eu-central-1.pooler.supabase.com:5432/postgres
   ```
6. Замени `[YOUR-PASSWORD]` с паролата от стъпка 1

---

## 4. Добави DATABASE_URL в Render (1 мин)

### Опция А — Render dashboard
1. Render → твоят web service → **Environment** (ляво меню)
2. **Add Environment Variable**:
   - Key: `DATABASE_URL`
   - Value: connection string-а от стъпка 3
3. **Save Changes** — Render автоматично ще пусне нов deploy

### Опция Б — Локално (за тест)
```bash
export DATABASE_URL='postgresql://postgres.xxx...@aws-0-eu-central-1.pooler.supabase.com:5432/postgres'
python3 execution/brand_scraper_server.py
```

---

## 5. Мигрирай съществуващи users (ако имаш `.tmp/users.json`)

Само ако вече имаш регистрирани users които искаш да запазиш:

```bash
# Локално с DATABASE_URL сетнат:
export DATABASE_URL='postgresql://...'
python3 execution/migrate_users_to_db.py
```

Скриптът чете `.tmp/users.json` и копира всеки user в Postgres. Idempotent — ако пуснеш пак, скипва вече мигрираните.

> **Не е задължително** ако започваш на чисто или продукционните users.json вече е в Render disk (миграцията може да се направи и от Render Shell).

---

## 6. Провери че работи

1. Деплой завърши → отвори app URL-а
2. Опитай се да регистрираш нов user → би трябвало да работи
3. Отвори Supabase Table Editor → **users** таблица → новият user е там ✓
4. Създай тестова advertorial / pixar job → виж дали се появява в **job_history**

### Тест на история API-то:
```bash
# С Bearer token от login response:
curl -H "Authorization: Bearer YOUR_TOKEN" \
     'https://YOUR-APP.onrender.com/api/history?limit=10'

# Filter по feature:
curl -H "Authorization: Bearer YOUR_TOKEN" \
     'https://YOUR-APP.onrender.com/api/history?feature=advertorial'
```

---

## Архитектура накратко

```
┌─────────────────────────────────────────────────┐
│  Render (Web Service — Starter $7/mo)           │
│  ┌──────────────────────────────────────────┐   │
│  │ brand_scraper_server.py                  │   │
│  │  • Auth → DB (users)                     │   │
│  │  • Jobs → filesystem (.tmp/*_jobs/)      │◄──┐│
│  │  • History → DB (job_history)            │   ││
│  └──────────────────────────────────────────┘   ││
│  ┌──────────────────────────────────────────┐   ││ (Render disk
│  │ .tmp/  (5GB persistent disk)             │◄──┘│   за job state)
│  │  • advertorial_jobs/<id>/state.json      │    │
│  │  • pixar_jobs/<id>/keyframes/*.jpg       │    │
│  └──────────────────────────────────────────┘    │
└──────────────────────┬──────────────────────────┘
                       │ DATABASE_URL
                       ▼
              ┌─────────────────────┐
              │  Supabase Postgres  │
              │  • users            │
              │  • job_history      │
              └─────────────────────┘
```

**Какво е в DB:** users + summary на всяка job (status, brief, result links, timestamps)
**Какво остава на disk:** job state файлове, images, video clips, audio — всичко с тежко binary съдържание

Това е първа фаза. Втора фаза: пълно DB migration + Supabase Storage за images.

---

## Troubleshooting

**"connection refused" в логовете** → провери DATABASE_URL формата, особено че `sslmode=require` е в края (Render го изисква)

**"FATAL: password authentication failed"** → паролата в connection string-а не е правилна. Регенерирай от Project Settings → Database → Reset Database Password

**"too many connections"** → твърде много restart-и на Render. Изчакай 1 мин или upgrade pool size в `execution/db.py` (line: `maxconn=10`)

**Users не се записват в DB но и не дава грешка** → проверяваш че DATABASE_URL е сетнат: `printenv DATABASE_URL`. Ако празно — fallback към filesystem.
