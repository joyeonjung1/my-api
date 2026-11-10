-- Supabase 대시보드 > SQL Editor 에서 한 번 실행하세요.
create table if not exists public.guestbook (
  id          bigint generated always as identity primary key,
  name        text not null check (char_length(name) between 1 and 30),
  place       text not null default '',
  message     text not null check (char_length(message) between 1 and 300),
  created_at  timestamptz not null default now()
);

-- 브라우저에서 직접 접근하지 못하도록 RLS를 켭니다.
-- FastAPI는 secret key를 쓰므로 RLS와 상관없이 읽고 쓸 수 있습니다.
alter table public.guestbook enable row level security;
