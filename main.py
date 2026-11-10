import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# 로컬에서는 .env를 읽고, Railway에서는 Variables에 등록한 값이 환경변수로 들어옴
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]
FRONT_ORIGINS = [
    o.strip() for o in os.getenv("FRONT_ORIGINS", "").split(",") if o.strip()
]

sb = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

app = FastAPI(title="my-api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONT_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TABLE = "guestbook"


class GuestbookIn(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    place: str = Field(default="", max_length=50)
    message: str = Field(min_length=1, max_length=300)


@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/v")
def health2():
    return {"s":"100", "t":"200"}

@app.get("/api/guestbook")
def list_guestbook(limit: int = Query(20, ge=1, le=100)):
    try:
        res = (
            sb.table(TABLE)
            .select("id, name, place, message, created_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase 조회 실패: {e}")
    return res.data


@app.post("/api/guestbook", status_code=201)
def create_guestbook(item: GuestbookIn):
    data = {k: v.strip() for k, v in item.model_dump().items()}
    if not data["name"] or not data["message"]:
        raise HTTPException(status_code=422, detail="이름과 내용은 비울 수 없습니다.")
    try:
        res = sb.table(TABLE).insert(data).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase 저장 실패: {e}")
    return res.data[0]
