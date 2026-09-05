# Bubbles Telegram Notifications

Готовый каркас Telegram-бота для уведомлений Bubbles.

Архитектура: Bubbles -> public.notifications -> Supabase Database Webhook -> /bubbles/webhook -> Telegram.

Supabase Database Webhooks поддерживают INSERT/UPDATE/DELETE и отправляют JSON с полями type/table/schema/record/old_record. https://supabase.com/docs/guides/database/webhooks

## Файлы
- bot.py — Telegram-бот + HTTP webhook receiver
- schema.sql — таблицы, RLS и RPC для одноразовой ссылки подключения
- bubbles-telegram.js — код кнопки "Подключить Telegram" в Bubbles
- requirements.txt — зависимости
- Dockerfile — запуск на хостинге
- .env.example — переменные окружения

## Запуск
1. Создай бота через @BotFather и получи TG_BOT_TOKEN.
2. Выполни schema.sql в Supabase SQL Editor.
3. Запусти сервер: `pip install -r requirements.txt` затем `uvicorn bot:app --host 0.0.0.0 --port 8080`.
4. Создай Supabase Database Webhook на `public.notifications`, событие INSERT, URL `https://YOUR_DOMAIN/bubbles/webhook`, header `X-Bubbles-Webhook-Secret: YOUR_LONG_SECRET`.
5. В bubbles-telegram.js замени YOUR_BOT_USERNAME.
6. Не помещай SUPABASE_SERVICE_ROLE_KEY в frontend или GitHub.

## Команды
/start — подключение
/settings — настройки уведомлений
/status — статус
/unlink — отключение

## Типы
message, comment, like, friend_request, friend_accept, mention, system
