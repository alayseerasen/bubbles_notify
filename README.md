# Bubbles — Telegram-уведомления (отдельный сервис)

В отличие от `supabase/functions/` (это Supabase Edge Functions,
живут внутри Supabase), этот бот — обычный Python-сервис
(FastAPI + python-telegram-bot), который нужно запустить отдельно и
держать постоянно включённым. Он одновременно:

1. Опрашивает Telegram (long polling) и обрабатывает `/start`,
   `/link`, `/settings`, `/status`, `/unlink`.
2. Принимает POST-запросы на `/bubbles/webhook` от Database Webhooks
   Supabase при появлении новых уведомлений/сообщений и пересылает их
   в Telegram.

Таблицы (`telegram_links`, `telegram_link_tokens`,
`notification_settings`) и функция `create_telegram_link_token()`
находятся в общем `supabase.sql` проекта, не здесь — прогони его как
обычно.

## 1. Создай бота в Telegram

1. Открой [@BotFather](https://t.me/BotFather) → `/newbot` → задай имя
   и юзернейм (должен заканчиваться на `bot`).
2. Сохрани токен вида `123456789:AA...`.
3. Впиши юзернейм бота (без `@`) в `js/telegram-config.js` в основном
   проекте Bubbles — это то, что видит пользователь в интерфейсе
   ("Откройте @ИМЯ_БОТА").

## 2. Задай переменные окружения

Скопируй `.env.example` в `.env` и заполни:

```
TG_BOT_TOKEN=<токен от BotFather>
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service_role ключ из Supabase → Settings → API>
BUBBLES_URL=https://<адрес твоего сайта Bubbles>
BUBBLES_WEBHOOK_SECRET=<придумай длинную случайную строку>
PORT=8080
LOG_LEVEL=INFO
```

⚠️ `SUPABASE_SERVICE_ROLE_KEY` даёт полный доступ к базе в обход RLS.
Это нормально — именно ради этого сервис и существует отдельно от
браузера — но храни его только в переменных окружения хостинга,
никогда не коммить `.env` и никогда не вставляй этот ключ ни в один
файл фронтенда.

## 3. Задеплой сервис

Любой хостинг, который умеет Dockerfile и постоянно работающий
процесс (не serverless/edge — боту нужен long polling), подойдёт:
Railway, Render (Background Worker или Web Service), Fly.io, свой
VPS. Дальше — на примере обычного Docker:

```
docker build -t bubbles-telegram-bot ./telegram-bot
docker run -d --env-file ./telegram-bot/.env -p 8080:8080 bubbles-telegram-bot
```

Проверь, что сервис жив:

```
curl https://<твой-домен-бота>/health
# {"ok":true,"service":"bubbles-telegram-notifications"}
```

## 4. Подключи два Database Webhook в Supabase

Dashboard → **Database** → **Webhooks** → **Create a new hook**,
дважды — оба указывают на один и тот же URL:

**Хук №1 — уведомления (лайки/комменты/заявки в друзья/стена/питомец):**
- Table: `bubbles_notifications`
- Events: `Insert`
- Type: **HTTP Request**
- URL: `https://<твой-домен-бота>/bubbles/webhook`
- HTTP Headers: `X-Bubbles-Webhook-Secret: <тот же секрет, что в BUBBLES_WEBHOOK_SECRET>`

**Хук №2 — сообщения:**
- Table: `messages`
- Events: `Insert`
- Type: **HTTP Request**
- URL: тот же `https://<твой-домен-бота>/bubbles/webhook`
- HTTP Headers: тот же `X-Bubbles-Webhook-Secret`

Без правильного заголовка сервис отвечает `401` и ничего не
пересылает — это и есть проверка, что запрос действительно пришёл от
твоего Supabase, а не от кого попало, кто узнал URL.

## 5. Проверка полного потока

1. Bubbles → Настройки профиля → **🔔 Telegram-уведомления** →
   «Подключить Telegram» — появится код вида `BUB-7K4Q9P`.
2. Либо нажми «Открыть бота» (сработает автоматически, код передастся
   через `/start`), либо вручную отправь боту `/link BUB-7K4Q9P`.
3. Бот ответит `✅ Telegram успешно подключён к вашему аккаунту
   Bubbles!`, а страница настроек Bubbles сама обновится на
   «🟢 Telegram подключён» в течение нескольких секунд.
4. `/settings` — переключи что-нибудь, попроси кого-нибудь лайкнуть
   пост или написать сообщение — должно прийти уведомление
   (или не прийти, если соответствующий тумблер выключен).
5. `/unlink` — должно прийти `🔕 Telegram отключён от Bubbles`, а
   Bubbles вернётся к состоянию «не подключено» после обновления.

## Если что-то не работает

- **Код не находится** — убедись, что обновлённый `supabase.sql`
  прогнан (нужны таблицы `telegram_link_tokens`/`telegram_links`/
  `notification_settings`), и что копируешь код без лишних пробелов.
- **"Код истёк"** — коды живут 10 минут, сгенерируй новый в Bubbles.
- **Бот не отвечает вообще** — смотри логи процесса (`docker logs` или
  логи хостинга). Если процесс не запущен/упал — polling не работает,
  сообщения от Telegram никуда не попадают.
- **Бот отвечает, но уведомления о лайках/сообщениях не приходят** —
  проверь оба Database Webhook: правильный ли URL, правильный ли
  заголовок `X-Bubbles-Webhook-Secret`, и нет ли `401`/`500` в логах
  сервиса при лайке/сообщении.
- **Уже подключён другой Telegram** — просто пришли `/link НОВЫЙ_КОД`
  (или открой бота по новой ссылке) из другого Telegram-аккаунта:
  старая связь автоматически заменяется новой.
- **Одно и то же уведомление пришло дважды** — не должно происходить:
  каждая строка помечается `telegram_sent_at` сразу после отправки, и
  сервис перепроверяет это поле заново из базы перед каждой отправкой
  (а не доверяет содержимому вебхука), так что повтор вебхука не
  дублирует сообщение. Если всё же случилось — это стоит расследовать
  как баг, а не игнорировать.
