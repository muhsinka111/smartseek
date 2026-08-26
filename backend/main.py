from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List
import hashlib, secrets, re, pathlib
import stripe

# ─────────────────────────────────────────────────────────────
# config — all from env; backend/.env supported (KEY=VALUE lines)
# swap SQLite for Postgres by setting DATABASE_URL at deploy
# ─────────────────────────────────────────────────────────────
import os

def _load_dotenv(path=".env"):
    try:
        for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass

_load_dotenv()

DATABASE_URL    = os.environ.get("DATABASE_URL", "sqlite:///./smartseek.db")
STRIPE_SECRET   = os.environ.get("STRIPE_SECRET_KEY", "")
PORT            = int(os.environ.get("PORT", "9091"))
# demo mode = no stripe key configured
DEMO_MODE = not bool(STRIPE_SECRET)
stripe.api_key = STRIPE_SECRET

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# ─────────────────────────────────────────────────────────────
# models
# ─────────────────────────────────────────────────────────────
class Position(Base):
    __tablename__ = "positions"
    id          = Column(Integer, primary_key=True, index=True)
    slug        = Column(String(120), unique=True, index=True, nullable=False)
    name        = Column(String(200), nullable=False)
    domain      = Column(String(200), nullable=False)
    description = Column(Text, default="")
    category    = Column(String(60), default="Platforms")
    color       = Column(String(16), default="#2563EB")
    is_demo     = Column(Boolean, default=False)
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    bids        = relationship("Bid", back_populates="position", order_by="desc(Bid.created_at)")
    clicks      = relationship("Click", back_populates="position")

class Bid(Base):
    __tablename__ = "bids"
    id          = Column(Integer, primary_key=True, index=True)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=False)
    amount      = Column(Integer, nullable=False)          # whole USD cents stored as integer
    status      = Column(String(20), default="pending")    # pending | paid | refunded
    stripe_pid  = Column(String(120), nullable=True)
    ip_hash     = Column(String(64), nullable=True)        # sha256 for fraud dedupe
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    paid_at     = Column(DateTime, nullable=True)
    position    = relationship("Position", back_populates="bids")

class Click(Base):
    __tablename__ = "clicks"
    id          = Column(Integer, primary_key=True, index=True)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=False)
    source      = Column(String(40), default="direct")     # x | google | linkedin | direct
    country     = Column(String(4),  default="--")
    referer     = Column(String(400), nullable=True)
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    position    = relationship("Position", back_populates="clicks")

class TradeLog(Base):
    __tablename__ = "trade_log"
    id          = Column(Integer, primary_key=True, index=True)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=False)
    delta       = Column(Integer, nullable=False)
    note        = Column(String(200), default="")
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))

Base.metadata.create_all(bind=engine)

# ─────────────────────────────────────────────────────────────
# pydantic schemas
# ─────────────────────────────────────────────────────────────
class PositionIn(BaseModel):
    name: str
    domain: str
    description: str = ""
    category: str = "Platforms"
    color: str = "#2563EB"

class BidIn(BaseModel):
    position_id: int
    amount: int = Field(ge=1)

class ClaimIn(BaseModel):
    domain: str
    name: str
    description: str = ""
    category: str = "Platforms"
    color: str = "#2563EB"
    amount: int = Field(ge=1)

# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def slugify(s: str) -> str:
    s = re.sub(r"https?://(www\\.)?", "", s).split("/")[0].split("?")[0]
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:100]

def total_paid(db: Session, position_id: int) -> int:
    return db.query(func.coalesce(func.sum(Bid.amount), 0)).filter(
        Bid.position_id == position_id, Bid.status == "paid"
    ).scalar() or 0

# ─────────────────────────────────────────────────────────────
# app
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="SmartSeek API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True, "demo": DEMO_MODE}

WEB_DIR = pathlib.Path(__file__).parent / "web"

@app.get("/")
def home():
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")

@app.get("/api/config")
def config():
    return {
        "demo": DEMO_MODE,
        "stripe_live": bool(STRIPE_SECRET),
        "positions": None,
    }

# ─────────────────────────────────────────────────────────────
# positions
# ─────────────────────────────────────────────────────────────
@app.get("/api/positions")
def list_positions(
    category: str = "",
    q: str = "",
    db: Session = Depends(get_db)
):
    qset = db.query(Position)
    if category:
        qset = qset.filter(Position.category == category)
    if q:
        like = f"%{q}%"
        qset = qset.filter(Position.name.ilike(like) | Position.domain.ilike(like))
    positions = qset.all()
    out = []
    for p in positions:
        paid = total_paid(db, p.id)
        clicks = db.query(Click).filter(Click.position_id == p.id).count()
        out.append({
            "id": p.id, "slug": p.slug, "name": p.name, "domain": p.domain,
            "description": p.description, "category": p.category, "color": p.color,
            "is_demo": p.is_demo, "created_at": p.created_at.isoformat(),
            "total_paid": paid, "clicks": clicks,
        })
    out.sort(key=lambda x: (-x["total_paid"], x["id"]))
    return out

@app.post("/api/positions")
def create_position(body: PositionIn, db: Session = Depends(get_db)):
    s = slugify(body.domain)
    if db.query(Position).filter(Position.slug == s).first():
        raise HTTPException(400, "Already listed — enter a bid instead")
    p = Position(slug=s, name=body.name, domain=body.domain,
                 description=body.description, category=body.category, color=body.color)
    db.add(p); db.flush()
    b = Bid(position_id=p.id, amount=1, status="paid")
    db.add(b); db.commit()
    return {"ok": True, "id": p.id, "slug": p.slug}

# ─────────────────────────────────────────────────────────────
# bids — the ranking engine
# ─────────────────────────────────────────────────────────────
@app.post("/api/bid")
def place_bid(body: BidIn, db: Session = Depends(get_db)):
    p = db.query(Position).filter(Position.id == body.position_id).first()
    if not p:
        raise HTTPException(404, "Position not found")
    if body.amount < 1:
        raise HTTPException(400, "Minimum bid is $1")
    current = total_paid(db, p.id)
    need = current + 1
    if body.amount < need:
        raise HTTPException(400, f"Must be at least ${need} to rank here")
    delta = body.amount - current
    b = Bid(position_id=p.id, amount=delta, status="paid", paid_at=datetime.now(timezone.utc))
    db.add(b)
    db.flush()
    db.add(TradeLog(position_id=p.id, delta=delta,
                    note=f"raised by ${delta} → total ${body.amount}"))
    db.commit()
    new_total = total_paid(db, p.id)
    rank = db.query(func.count(func.distinct(Position.id))).join(Bid).filter(
        Bid.status == "paid", Bid.amount > new_total
    ).scalar() + 1
    return {"ok": True, "rank": rank, "total_paid": new_total, "position_id": p.id, "delta": delta}

@app.post("/api/claim")
def claim_top(body: ClaimIn, db: Session = Depends(get_db)):
    """
    Claim №1: pay enough to beat the current leader.
    Returns a Stripe checkout session when STRIPE_SECRET is set,
    otherwise a demo placeholder.
    """
    s = slugify(body.domain)
    existing = db.query(Position).filter(Position.slug == s).first()
    if existing:
        p = existing
    else:
        p = Position(slug=s, name=body.name, domain=body.domain,
                     description=body.description, category=body.category, color=body.color)
        db.add(p); db.flush()

    # find the highest total_paid across all positions
    leader_total = db.query(func.coalesce(func.sum(Bid.amount), 0)).join(Position).filter(
        Bid.status == "paid"
    ).scalar() or 0

    current = total_paid(db, p.id)
    need = max(leader_total + 1, body.amount, current + 1)
    delta = need - current

    b = Bid(position_id=p.id, amount=delta, status="pending",
            created_at=datetime.now(timezone.utc))
    db.add(b); db.commit()

    if STRIPE_SECRET:
        base = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:9091")
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price_data": {"currency": "usd", "unit_amount": delta * 100,
                                       "product_data": {"name": f"Rank №1 — {p.name} (total ${need})"}},
                         "quantity": 1}],
            mode="payment",
            success_url=f"{base}/success?bid_id={b.id}",
            cancel_url=f"{base}/claim",
            metadata={"bid_id": str(b.id), "position_id": str(p.id)},
        )
        b.stripe_pid = session.id
        db.commit()
        return {"checkout_url": session.url, "demo": False, "bid_id": b.id}
    else:
        # DEMO fallback — auto-confirm after 1.2s from front-end
        return {"checkout_url": None, "demo": True, "bid_id": b.id,
                "amount": need, "position_id": p.id, "delta": delta}


# ─────────────────────────────────────────────────────────────
# stripe webhook — real payments land here
# ─────────────────────────────────────────────────────────────
from fastapi import Request

@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    whsec = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    event = None
    if whsec and sig:
        try:
            event = stripe.Webhook.construct_event(payload, sig, whsec)
        except Exception:
            raise HTTPException(400, "Invalid signature")
    else:
        # test mode convenience: unsigned payloads accepted when no whsec configured
        import json as _json
        try:
            event = _json.loads(payload)
        except Exception:
            raise HTTPException(400, "Bad payload")

    if event.get("type") == "checkout.session.completed":
        obj = event["data"]["object"]
        meta = obj.get("metadata") or {}
        bid_id = int(meta.get("bid_id", 0) or 0)
        b = db.query(Bid).filter(Bid.id == bid_id).first() if bid_id else None
        if b and b.status != "paid":
            b.status = "paid"
            b.paid_at = datetime.now(timezone.utc)
            b.stripe_pid = obj.get("id")
            db.add(TradeLog(position_id=b.position_id, delta=b.amount,
                            note="payment confirmed (stripe)"))
            db.commit()
    return {"received": True}

@app.post("/api/bid/{bid_id}/confirm")
def confirm_bid_demo(bid_id: int, db: Session = Depends(get_db)):
    b = db.query(Bid).filter(Bid.id == bid_id).first()
    if not b:
        raise HTTPException(404, "Bid not found")
    if b.status == "paid":
        return {"ok": True, "already": True}
    b.status = "paid"
    b.paid_at = datetime.now(timezone.utc)
    db.add(TradeLog(position_id=b.position_id, delta=b.amount,
                    note="payment confirmed (demo)"))
    db.commit()
    new_total = total_paid(db, b.position_id)
    rank = db.query(func.count(func.distinct(Position.id))).join(Bid).filter(
        Bid.status == "paid", Bid.amount > new_total
    ).scalar() + 1
    return {"ok": True, "rank": rank, "total_paid": new_total, "position_id": b.position_id}

# ─────────────────────────────────────────────────────────────
# click redirect — honest counting
# ─────────────────────────────────────────────────────────────
@app.get("/r/{slug}")
def redirect_slug(slug: str, db: Session = Depends(get_db)):
    p = db.query(Position).filter(Position.slug == slug).first()
    if not p:
        raise HTTPException(404, "Not found")
    c = Click(position_id=p.id, source="direct", country="--")
    db.add(c); db.commit()
    # in prod replace with real URL; for demo use https example
    dest = "https://example.com" if p.domain.startswith("http") else f"https://{p.domain}"
    return RedirectResponse(dest, status_code=302)

@app.get("/api/clicks/{position_id}")
def clicks_for(position_id: int, db: Session = Depends(get_db)):
    p = db.query(Position).filter(Position.id == position_id).first()
    if not p:
        raise HTTPException(404, "Not found")
    clicks = db.query(Click).filter(Click.position_id == position_id).count()
    by_country = dict(db.query(Click.country, func.count(Click.id))
                      .filter(Click.position_id == position_id)
                      .group_by(Click.country).all())
    return {"position_id": position_id, "total": clicks, "by_country": by_country}

# ─────────────────────────────────────────────────────────────
# analytics summary
# ─────────────────────────────────────────────────────────────
@app.get("/api/analytics/summary")
def analytics_summary(db: Session = Depends(get_db)):
    total_positions = db.query(Position).count()
    total_clicks = db.query(Click).count()
    pot = db.query(func.coalesce(func.sum(Bid.amount), 0)).filter(Bid.status == "paid").scalar() or 0
    seats_today = db.query(Position).filter(
        Position.created_at >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
    ).count()
    return {
        "positions": total_positions,
        "clicks": total_clicks,
        "pot": pot,
        "seats_today": seats_today,
    }

@app.get("/api/activity")
def activity(limit: int = Query(14, le=50), db: Session = Depends(get_db)):
    """Public ledger tail: latest trades for the activity feed."""
    rows = db.query(TradeLog, Position).join(
        Position, TradeLog.position_id == Position.id
    ).order_by(TradeLog.created_at.desc()).limit(limit).all()
    return [{
        "position_id": t.position_id,
        "name": p.name,
        "color": p.color,
        "delta": t.delta,
        "note": t.note,
        "ts": t.created_at.isoformat(),
    } for t, p in rows]

# ─────────────────────────────────────────────────────────────
# seed
# ─────────────────────────────────────────────────────────────
@app.post("/api/seed")
def seed_demo(db: Session = Depends(get_db)):
    if db.query(Position).filter(Position.is_demo == True).count() > 0:
        return {"ok": True, "message": "demo data already present"}
    demo = [
        ("smartseek",    "SmartSeek",           "smartseek.com",     "The marketing platform for intelligent things. AI tools, agents, robots, people, businesses — ranked by what backers paid. No account. No API keys. Pay, rank, get traffic.", "Platforms",    "#2563EB",   1),
        ("ortaq",        "Ortaq",               "ortaq.biz",          "AI agents that work alongside your team — sales, support, research. Autonomous employees that learn and adapt.", "Agents",      "#0F172A",   2),
        ("openai",       "OpenAI",              "openai.com",         "ChatGPT, GPT-4, and the research behind them. The company that defined modern AI.", "AI Tools",    "#10A37F", 500),
        ("anthropic",    "Anthropic",           "anthropic.com",      "Claude and the pursuit of reliable, interpretable AI. Safety-first reasoning models.", "AI Tools",    "#D97706", 400),
        ("ihalezeka",    "İhaleZeka",           "ihalezeka.com",      "AI-powered tender intelligence for Turkish public procurement. SAM.gov + TED integration. 6 years of live data.", "Platforms",    "#2563EB", 300),
        ("luxrentals",   "Lux Rentals",         "luxrentals.com",     "Curated short-term luxury properties. Verified hosts, instant booking, concierge service.", "Businesses",  "#7C3AED", 150),
        ("7stories",     "7Stories",            "7stories.com",       "Short-form vertical video platform. AI-curated stories, creator monetization, viral discovery.", "Platforms",    "#DB2777", 120),
        ("github",       "GitHub",              "github.com",         "Where the world builds software. 100M+ developers, AI copilot, the open-source backbone.", "Platforms",    "#24292E", 200),
        ("huggingface",  "Hugging Face",        "huggingface.co",     "The AI community platform. 500k+ models, datasets, and spaces — open ML for everyone.", "AI Tools",    "#FFD21E", 180),
        ("perplexity",   "Perplexity",          "perplexity.ai",      "AI-powered answer engine. Search the web with a knowledgeable AI that cites its sources.", "AI Tools",    "#2563EB", 250),
    ]
    for slug, name, domain, desc, cat, color, bid in demo:
        p = Position(slug=slug, name=name, domain=domain, description=desc,
                     category=cat, color=color, is_demo=True)
        db.add(p); db.flush()
        b = Bid(position_id=p.id, amount=bid, status="paid",
                paid_at=datetime.now(timezone.utc))
        db.add(b)
    db.commit()
    return {"ok": True, "seeded": len(demo)}

@app.post("/api/reset")
def reset_and_seed(db: Session = Depends(get_db)):
    """Demo-mode only: wipe everything and reseed with real starter listings."""
    if not DEMO_MODE:
        raise HTTPException(403, "Reset disabled outside demo mode")
    db.query(TradeLog).delete()
    db.query(Click).delete()
    db.query(Bid).delete()
    db.query(Position).delete()
    db.commit()
    return seed_demo(db)

# Serve built front-end from backend/web/ (brutalist landing page)
from pathlib import Path as _Path
FRONTEND_DIR = _Path(__file__).resolve().parent / "web"

@app.get("/{full_path:path}")
def serve_frontend(request: Request, full_path: str):
    rel = full_path.lstrip("/")
    target = FRONTEND_DIR / rel if rel else FRONTEND_DIR / "index.html"
    if target.is_file() and target.suffix in {".html",".js",".css",".png",".svg",".ico",".json",".txt",".xml",".map"}:
        from fastapi.responses import FileResponse
        return FileResponse(target)
    idx = FRONTEND_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    raise HTTPException(404)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
