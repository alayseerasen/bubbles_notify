create extension if not exists pgcrypto;

create table if not exists public.telegram_links (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  telegram_chat_id bigint not null unique,
  telegram_username text,
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(user_id)
);
create index if not exists telegram_links_user_id_idx on public.telegram_links(user_id);

create table if not exists public.telegram_link_tokens (
  token text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  expires_at timestamptz not null default (now() + interval '10 minutes'),
  created_at timestamptz not null default now()
);
create index if not exists telegram_link_tokens_user_idx on public.telegram_link_tokens(user_id);

create table if not exists public.notification_settings (
  user_id uuid primary key references auth.users(id) on delete cascade,
  messages_enabled boolean not null default true,
  comments_enabled boolean not null default true,
  likes_enabled boolean not null default true,
  friends_enabled boolean not null default true,
  updated_at timestamptz not null default now()
);

create table if not exists public.notifications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  type text not null,
  actor_user_id uuid references auth.users(id) on delete set null,
  actor_name text,
  actor_username text,
  entity_id text,
  content text,
  created_at timestamptz not null default now(),
  telegram_sent_at timestamptz
);
create index if not exists notifications_user_created_idx on public.notifications(user_id, created_at desc);
create index if not exists notifications_pending_idx on public.notifications(created_at) where telegram_sent_at is null;

alter table public.telegram_links enable row level security;
alter table public.telegram_link_tokens enable row level security;
alter table public.notification_settings enable row level security;
alter table public.notifications enable row level security;

revoke all on public.telegram_links from anon, authenticated;
revoke all on public.telegram_link_tokens from anon, authenticated;

create policy if not exists "notification_settings_select_own" on public.notification_settings for select to authenticated using ((select auth.uid()) = user_id);
create policy if not exists "notification_settings_insert_own" on public.notification_settings for insert to authenticated with check ((select auth.uid()) = user_id);
create policy if not exists "notification_settings_update_own" on public.notification_settings for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy if not exists "notifications_select_own" on public.notifications for select to authenticated using ((select auth.uid()) = user_id);
create policy if not exists "notifications_insert_own" on public.notifications for insert to authenticated with check ((select auth.uid()) = user_id);

create or replace function public.create_telegram_link_token()
returns text
language plpgsql
set search_path = public
as $$
declare
  new_token text;
  current_user_id uuid := auth.uid();
begin
  if current_user_id is null then raise exception 'not authenticated'; end if;
  delete from public.telegram_link_tokens where user_id = current_user_id or expires_at < now();
  new_token := encode(gen_random_bytes(24), 'hex');
  insert into public.telegram_link_tokens(token, user_id) values (new_token, current_user_id);
  return new_token;
end;
$$;
grant execute on function public.create_telegram_link_token() to authenticated;

-- После событий Bubbles создавай уведомление для ПОЛУЧАТЕЛЯ:
-- insert into public.notifications (user_id,type,actor_user_id,actor_name,actor_username,entity_id,content)
-- values (recipient_user_id,'message',auth.uid(),sender_display_name,sender_username,message_id::text,message_text);
-- Аналогично type='comment'/'like'/'friend_request'/'friend_accept'/'mention'/'system'.
