import logging
import os
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import Lock

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from supabase import create_client

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("my-api")

# ---------------------------------------------------------------------------
# 환경변수
#   로컬: my-api/.env 파일에서 읽음
#   Railway: Variables 탭에 등록한 값이 환경변수로 들어옴
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]  # 절대 프론트엔드/깃허브에 노출 금지

# 접근을 허용할 프론트엔드 주소 (쉼표 구분, 정확한 주소만. 와일드카드 '*' 금지)
#   예) FRONT_ORIGINS=https://my-shop.vercel.app,http://localhost:3000
FRONT_ORIGINS = [
    o.strip().rstrip("/") for o in os.getenv("FRONT_ORIGINS", "").split(",") if o.strip()
]
if "*" in FRONT_ORIGINS:
    raise RuntimeError("FRONT_ORIGINS에 '*'는 사용할 수 없습니다. 정확한 주소를 적어 주세요.")

# API 문서(/docs)는 로컬에서만 켬. Railway에서는 설정하지 않으면 꺼짐
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "0") == "1"

# 요청 제한: IP당 1분에 허용하는 요청 수
RATE_LIMIT_READ = int(os.getenv("RATE_LIMIT_READ", "120"))
RATE_LIMIT_WRITE = int(os.getenv("RATE_LIMIT_WRITE", "30"))

sb = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

app = FastAPI(
    title="my-api",
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)

FREE_SHIPPING_MIN = 30000
SHIPPING_FEE = 3000
MAX_CART_LINES = 30
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------------------
# 보안 미들웨어
# ---------------------------------------------------------------------------
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = Lock()


def client_ip(request: Request) -> str:
    # Railway 프록시는 실제 클라이언트 IP를 X-Forwarded-For 끝에 붙임.
    # 앞부분은 클라이언트가 위조할 수 있으므로 마지막 값을 사용
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def rate_limited(key: str, limit: int, window: float = 60.0) -> bool:
    now = time.monotonic()
    with _hits_lock:
        q = _hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        if len(_hits) > 50_000:  # 메모리 보호
            _hits.clear()
        return False


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    method = request.method
    if method != "OPTIONS":
        is_write = method in WRITE_METHODS

        # 1) 데이터를 바꾸는 요청은 허용된 프론트엔드에서 온 것만 받음
        #    (CORS는 브라우저만 막으므로, 서버에서도 Origin을 한 번 더 확인)
        if is_write and request.headers.get("origin", "").rstrip("/") not in FRONT_ORIGINS:
            return JSONResponse({"detail": "허용되지 않은 출처입니다."}, status_code=403)

        # 2) IP별 요청 횟수 제한
        ip = client_ip(request)
        limit = RATE_LIMIT_WRITE if is_write else RATE_LIMIT_READ
        if rate_limited(f"{ip}:{'w' if is_write else 'r'}", limit):
            return JSONResponse(
                {"detail": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."},
                status_code=429,
                headers={"Retry-After": "60"},
            )

        # 3) 너무 큰 요청 본문 차단 (16KB)
        if int(request.headers.get("content-length") or 0) > 16 * 1024:
            return JSONResponse({"detail": "요청이 너무 큽니다."}, status_code=413)

    response = await call_next(request)

    # 4) 보안 헤더
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# CORS: 가장 바깥에 둬야 429/403 응답에도 CORS 헤더가 붙음 (나중에 추가 = 바깥)
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONT_ORIGINS,
    allow_credentials=False,  # 쿠키를 쓰지 않으므로 끔
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-Cart-Id"],
    max_age=600,
)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    # 내부 오류 내용(DB 구조, 키 등)은 서버 로그에만 남기고 사용자에게는 숨김
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "서버 오류가 발생했습니다."}, status_code=500)


def db_error(action: str, e: Exception) -> HTTPException:
    log.error("Supabase %s 실패: %s", action, e)
    return HTTPException(status_code=500, detail=f"{action} 중 서버 오류가 발생했습니다.")


# ---------------------------------------------------------------------------
# 기본
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v")
def health2():
    return {"s": "100", "t": "200"}


# ---------------------------------------------------------------------------
# 방명록
# ---------------------------------------------------------------------------
class GuestbookIn(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    place: str = Field(default="", max_length=50)
    message: str = Field(min_length=1, max_length=300)


@app.get("/api/guestbook")
def list_guestbook(limit: int = Query(20, ge=1, le=100)):
    try:
        res = (
            sb.table("guestbook")
            .select("id, name, place, message, created_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as e:
        raise db_error("방명록 조회", e)
    return res.data


@app.post("/api/guestbook", status_code=201)
def create_guestbook(item: GuestbookIn):
    data = {k: v.strip() for k, v in item.model_dump().items()}
    if not data["name"] or not data["message"]:
        raise HTTPException(status_code=422, detail="이름과 내용은 비울 수 없습니다.")
    try:
        res = sb.table("guestbook").insert(data).execute()
    except Exception as e:
        raise db_error("방명록 저장", e)
    return res.data[0]


# ---------------------------------------------------------------------------
# 상품 / 장바구니 / 주문
# ---------------------------------------------------------------------------
PRODUCT_FIELDS = "id, name, description, category, price, emoji, color, stock"


def get_cart_id(x_cart_id: str = Header(..., alias="X-Cart-Id")) -> str:
    """브라우저마다 발급한 익명 장바구니 ID(UUID v4)만 허용"""
    try:
        cid = uuid.UUID(x_cart_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="잘못된 장바구니 ID입니다.")
    if cid.version != 4:
        raise HTTPException(status_code=400, detail="잘못된 장바구니 ID입니다.")
    return str(cid)


class CartAdd(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(default=1, ge=1, le=99)


class CartUpdate(BaseModel):
    quantity: int = Field(ge=1, le=99)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_active_product(product_id: int) -> dict:
    try:
        res = (
            sb.table("products")
            .select(PRODUCT_FIELDS)
            .eq("id", product_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise db_error("상품 조회", e)
    if not res.data:
        raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다.")
    return res.data[0]


def build_cart(cart_id: str) -> dict:
    """가격·합계는 항상 서버에서 DB 가격으로 계산 (프론트가 보낸 금액은 믿지 않음)"""
    try:
        res = (
            sb.table("cart_items")
            .select(f"product_id, quantity, products({PRODUCT_FIELDS}, is_active)")
            .eq("cart_id", cart_id)
            .order("created_at")
            .execute()
        )
    except Exception as e:
        raise db_error("장바구니 조회", e)

    items = []
    for row in res.data:
        p = row.get("products")
        if not p or not p.get("is_active"):
            continue
        p.pop("is_active", None)
        items.append(
            {
                "product": p,
                "quantity": row["quantity"],
                "line_total": p["price"] * row["quantity"],
            }
        )
    subtotal = sum(i["line_total"] for i in items)
    shipping = 0 if subtotal == 0 or subtotal >= FREE_SHIPPING_MIN else SHIPPING_FEE
    return {
        "items": items,
        "item_count": sum(i["quantity"] for i in items),
        "subtotal": subtotal,
        "shipping": shipping,
        "total": subtotal + shipping,
        "free_shipping_min": FREE_SHIPPING_MIN,
    }


@app.get("/api/products")
def list_products(category: str | None = Query(None, max_length=20)):
    try:
        q = sb.table("products").select(PRODUCT_FIELDS).eq("is_active", True).order("id")
        if category:
            q = q.eq("category", category)
        res = q.execute()
    except Exception as e:
        raise db_error("상품 목록 조회", e)
    return res.data


@app.get("/api/cart")
def get_cart(cart_id: str = Depends(get_cart_id)):
    return build_cart(cart_id)


@app.post("/api/cart/items", status_code=201)
def add_to_cart(body: CartAdd, cart_id: str = Depends(get_cart_id)):
    product = get_active_product(body.product_id)
    try:
        existing = (
            sb.table("cart_items")
            .select("id, quantity")
            .eq("cart_id", cart_id)
            .eq("product_id", body.product_id)
            .limit(1)
            .execute()
        ).data
        if existing:
            new_qty = min(existing[0]["quantity"] + body.quantity, 99, product["stock"])
            if new_qty < 1:
                raise HTTPException(status_code=409, detail="재고가 없습니다.")
            sb.table("cart_items").update(
                {"quantity": new_qty, "updated_at": now_iso()}
            ).eq("id", existing[0]["id"]).execute()
        else:
            lines = (
                sb.table("cart_items")
                .select("id", count="exact")
                .eq("cart_id", cart_id)
                .execute()
            ).count or 0
            if lines >= MAX_CART_LINES:
                raise HTTPException(status_code=409, detail="장바구니에 담을 수 있는 상품 종류를 초과했습니다.")
            qty = min(body.quantity, product["stock"])
            if qty < 1:
                raise HTTPException(status_code=409, detail="재고가 없습니다.")
            sb.table("cart_items").insert(
                {"cart_id": cart_id, "product_id": body.product_id, "quantity": qty}
            ).execute()
    except HTTPException:
        raise
    except Exception as e:
        raise db_error("장바구니 담기", e)
    return build_cart(cart_id)


@app.patch("/api/cart/items/{product_id}")
def update_cart_item(
    body: CartUpdate,
    product_id: int = Path(gt=0),
    cart_id: str = Depends(get_cart_id),
):
    product = get_active_product(product_id)
    if body.quantity > product["stock"]:
        raise HTTPException(status_code=409, detail=f"재고가 {product['stock']}개 남았습니다.")
    try:
        res = (
            sb.table("cart_items")
            .update({"quantity": body.quantity, "updated_at": now_iso()})
            .eq("cart_id", cart_id)
            .eq("product_id", product_id)
            .execute()
        )
    except Exception as e:
        raise db_error("수량 변경", e)
    if not res.data:
        raise HTTPException(status_code=404, detail="장바구니에 없는 상품입니다.")
    return build_cart(cart_id)


@app.delete("/api/cart/items/{product_id}")
def remove_cart_item(product_id: int = Path(gt=0), cart_id: str = Depends(get_cart_id)):
    try:
        sb.table("cart_items").delete().eq("cart_id", cart_id).eq("product_id", product_id).execute()
    except Exception as e:
        raise db_error("상품 삭제", e)
    return build_cart(cart_id)


@app.delete("/api/cart")
def clear_cart(cart_id: str = Depends(get_cart_id)):
    try:
        sb.table("cart_items").delete().eq("cart_id", cart_id).execute()
    except Exception as e:
        raise db_error("장바구니 비우기", e)
    return build_cart(cart_id)


@app.post("/api/checkout", status_code=201)
def checkout(cart_id: str = Depends(get_cart_id)):
    try:
        res = sb.rpc("checkout_cart", {"p_cart_id": cart_id}).execute()
    except Exception as e:
        msg = str(e)
        if "EMPTY_CART" in msg:
            raise HTTPException(status_code=400, detail="장바구니가 비어 있습니다.")
        if "OUT_OF_STOCK" in msg:
            raise HTTPException(status_code=409, detail="재고가 부족한 상품이 있습니다.")
        raise db_error("주문", e)
    row = res.data[0]
    return {
        "order_id": row["out_order_id"],
        "total": row["out_total"],
        "item_count": row["out_item_count"],
    }
