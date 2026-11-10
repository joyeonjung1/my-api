-- =====================================================================
-- 제주 마켓 장바구니 스키마
-- Supabase 대시보드 > SQL Editor 에 전체를 붙여 넣고 Run 하세요.
-- 여러 번 실행해도 안전합니다.
-- =====================================================================

-- 1) 상품 -------------------------------------------------------------
create table if not exists public.products (
  id          bigint generated always as identity primary key,
  name        text    not null check (char_length(name) between 1 and 60),
  description text    not null default '',
  category    text    not null default '기타',
  price       integer not null check (price >= 0),
  emoji       text    not null default '🛍️',
  color       text    not null default '#f97316' check (color ~ '^#[0-9a-fA-F]{6}$'),
  stock       integer not null default 100 check (stock >= 0),
  is_active   boolean not null default true,
  created_at  timestamptz not null default now()
);

-- 2) 장바구니 (로그인 없이 브라우저마다 발급되는 익명 cart_id 사용) ----
create table if not exists public.cart_items (
  id          bigint generated always as identity primary key,
  cart_id     uuid    not null,
  product_id  bigint  not null references public.products(id) on delete cascade,
  quantity    integer not null check (quantity between 1 and 99),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (cart_id, product_id)
);
create index if not exists cart_items_cart_id_idx on public.cart_items (cart_id);

-- 3) 주문 -------------------------------------------------------------
create table if not exists public.orders (
  id          bigint generated always as identity primary key,
  cart_id     uuid    not null,
  subtotal    integer not null,
  shipping    integer not null,
  total       integer not null,
  item_count  integer not null,
  status      text    not null default 'placed',
  created_at  timestamptz not null default now()
);

create table if not exists public.order_items (
  id            bigint generated always as identity primary key,
  order_id      bigint  not null references public.orders(id) on delete cascade,
  product_id    bigint  references public.products(id) on delete set null,
  product_name  text    not null,
  unit_price    integer not null,
  quantity      integer not null
);

-- 4) 보안: RLS를 켜고 정책을 만들지 않음 ------------------------------
--    → 브라우저용 anon/publishable 키로는 아무것도 읽거나 쓸 수 없음
--    → 오직 서버(FastAPI)의 secret key만 접근 가능
alter table public.products    enable row level security;
alter table public.cart_items  enable row level security;
alter table public.orders      enable row level security;
alter table public.order_items enable row level security;

revoke all on public.products, public.cart_items, public.orders, public.order_items
  from anon, authenticated;

-- 5) 주문 처리 함수 (금액은 반드시 DB 가격으로 계산, 한 트랜잭션으로 처리)
create or replace function public.checkout_cart(p_cart_id uuid)
returns table (out_order_id bigint, out_total integer, out_item_count integer)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_order_id bigint;
  v_subtotal integer;
  v_shipping integer;
  v_count    integer;
begin
  -- 동시 주문 시 재고가 꼬이지 않도록 상품 행 잠금
  perform 1
    from public.products p
    join public.cart_items c on c.product_id = p.id
   where c.cart_id = p_cart_id
     for update of p;

  select coalesce(sum(p.price * c.quantity), 0), coalesce(sum(c.quantity), 0)
    into v_subtotal, v_count
    from public.cart_items c
    join public.products p on p.id = c.product_id
   where c.cart_id = p_cart_id and p.is_active;

  if v_count = 0 then
    raise exception 'EMPTY_CART';
  end if;

  if exists (
    select 1 from public.cart_items c
      join public.products p on p.id = c.product_id
     where c.cart_id = p_cart_id and p.is_active and c.quantity > p.stock
  ) then
    raise exception 'OUT_OF_STOCK';
  end if;

  v_shipping := case when v_subtotal >= 30000 then 0 else 3000 end;

  insert into public.orders (cart_id, subtotal, shipping, total, item_count)
  values (p_cart_id, v_subtotal, v_shipping, v_subtotal + v_shipping, v_count)
  returning id into v_order_id;

  insert into public.order_items (order_id, product_id, product_name, unit_price, quantity)
  select v_order_id, p.id, p.name, p.price, c.quantity
    from public.cart_items c
    join public.products p on p.id = c.product_id
   where c.cart_id = p_cart_id and p.is_active;

  update public.products p
     set stock = p.stock - c.quantity
    from public.cart_items c
   where c.cart_id = p_cart_id and c.product_id = p.id and p.is_active;

  delete from public.cart_items where cart_id = p_cart_id;

  return query select v_order_id, v_subtotal + v_shipping, v_count;
end;
$$;

-- 함수는 서버(service_role)만 실행 가능
revoke execute on function public.checkout_cart(uuid) from public, anon, authenticated;
grant  execute on function public.checkout_cart(uuid) to service_role;

-- 6) 샘플 상품 (상품이 하나도 없을 때만 추가) ------------------------
insert into public.products (name, description, category, price, emoji, color, stock)
select * from (values
  ('노지 감귤 3kg',     '햇살 듬뿍 받은 새콤달콤 제주 감귤',      '과일',   19900, '🍊', '#fb923c', 120),
  ('한라봉 2kg',        '꼭지가 볼록, 진한 단맛의 겨울 별미',      '과일',   32000, '🍊', '#f97316',  60),
  ('청귤청 500ml',      '탄산수에 타면 상큼한 청귤에이드',         '음료',   15000, '🍋', '#84cc16',  80),
  ('제주 녹차 티백 20입','서귀포 다원에서 덖은 맑은 녹차',          '음료',   12000, '🍵', '#16a34a',  90),
  ('오메기떡 10입',     '차조와 팥고물로 빚은 제주 전통 떡',       '간식',   18000, '🍡', '#a16207',  50),
  ('감귤 초콜릿',       '여행 선물 1순위, 감귤향 화이트 초콜릿',   '간식',    9900, '🍫', '#ea580c', 200),
  ('우도 땅콩 과자',    '고소함이 남다른 우도 땅콩으로 만든 과자', '간식',    8500, '🥜', '#ca8a04', 150),
  ('흑돼지 오겹살 600g','쫄깃한 식감의 제주 흑돼지 (냉장)',        '특산품', 27000, '🥩', '#be123c',  40),
  ('제주 옥돔 2미',     '손질 후 반건조한 은빛 옥돔',              '특산품', 35000, '🐟', '#0e7490',  30),
  ('돌하르방 마그넷',   '냉장고에 붙이는 작은 제주',               '기념품',  6000, '🗿', '#475569', 300)
) as v(name, description, category, price, emoji, color, stock)
where not exists (select 1 from public.products);
