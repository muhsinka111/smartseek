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
import hashlib, secrets, re, pathlib, os
import stripe

# ─────────────────────────────────────────────────────────────
# config — all from env; backend/.env supported (KEY=VALUE lines)
# swap SQLite for Postgres by setting DATABASE_URL at deploy
# ─────────────────────────────────────────────────────────────
import os as _os

def _load_dotenv(path=".env"):
    try:
        for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                _os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass

_load_dotenv()

DATABASE_URL    = _os.environ.get("DATABASE_URL", "sqlite:///./smartseek.db")
STRIPE_SECRET   = _os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = _os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PUBLIC_BASE_URL = _os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:9091")
GA4_MEASUREMENT_ID = _os.environ.get("GA4_MEASUREMENT_ID", "")
PORT            = int(_os.environ.get("PORT", "9091"))
MAX_BID         = 100_000
CATEGORY_CATALOG = [
    {"slug": "ai-tools", "name": "AI Tools"},
    {"slug": "ai-agents", "name": "AI Agents"},
    {"slug": "marketing", "name": "Marketing"},
    {"slug": "data-research", "name": "Data & Research"},
    {"slug": "saas", "name": "SaaS"},
    {"slug": "developer-tools", "name": "Developer Tools"},
    {"slug": "automation", "name": "Automation"},
    {"slug": "robotics", "name": "Robotics"},
    {"slug": "devices", "name": "Devices"},
    {"slug": "other", "name": "Other"},
]
ALLOWED_CATEGORIES = {x["name"] for x in CATEGORY_CATALOG}
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
    amount      = Column(Integer, nullable=False)
    status      = Column(String(20), default="pending")
    stripe_pid  = Column(String(120), nullable=True)
    ip_hash     = Column(String(64), nullable=True)
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    paid_at     = Column(DateTime, nullable=True)
    position    = relationship("Position", back_populates="bids")

class Click(Base):
    __tablename__ = "clicks"
    id          = Column(Integer, primary_key=True, index=True)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=False)
    source      = Column(String(40), default="direct")
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

class AppSetting(Base):
    __tablename__ = "app_settings"
    key         = Column(String(120), primary_key=True)
    value       = Column(String(400), nullable=False, default="")

Base.metadata.create_all(bind=engine)

# ─────────────────────────────────────────────────────────────
# pydantic schemas
# ─────────────────────────────────────────────────────────────
class PositionIn(BaseModel):
    name: str
    domain: str
    description: str = ""
    category: str = "Other"
    color: str = "#2563EB"

class BidIn(BaseModel):
    position_id: int
    amount: int = Field(ge=1, le=MAX_BID)

class ClaimIn(BaseModel):
    domain: str
    name: str
    description: str = ""
    category: str = "Other"
    color: str = "#2563EB"
    amount: int = Field(ge=1, le=MAX_BID)

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
    s = str(s or "").strip()
    if s.startswith("@"):
        s = "x.com/" + s[1:]
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = s.split("?")[0].split("#")[0].strip("/")
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:100]

def total_paid(db: Session, position_id: int) -> int:
    return db.query(func.coalesce(func.sum(Bid.amount), 0)).filter(
        Bid.position_id == position_id, Bid.status == "paid"
    ).scalar() or 0

def paid_totals(db: Session) -> dict:
    rows = db.query(Bid.position_id, func.coalesce(func.sum(Bid.amount), 0)).filter(
        Bid.status == "paid"
    ).group_by(Bid.position_id).all()
    return {position_id: int(total or 0) for position_id, total in rows}

def rank_for(db: Session, position_id: int, proposed_total: Optional[int] = None) -> int:
    totals = paid_totals(db)
    if proposed_total is not None:
        totals[position_id] = int(proposed_total)
    positions = db.query(Position).filter(Position.id.in_(list(totals.keys()))).all() if totals else []
    order = {p.id: p.id for p in positions}  # id is creation order; older wins ties
    ranked = sorted(totals, key=lambda pid: (-totals[pid], order.get(pid, pid)))
    return ranked.index(position_id) + 1 if position_id in ranked else len(ranked) + 1

def rank_for_category(db: Session, position_id: int, proposed_total: Optional[int] = None) -> int:
    """Rank inside the listing's category while preserving global ranking."""
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        return 1
    totals = paid_totals(db)
    if proposed_total is not None:
        totals[position_id] = int(proposed_total)
    ids = [pid for pid in totals if db.query(Position).filter(
        Position.id == pid, Position.category == position.category
    ).first()]
    ranked = sorted(ids, key=lambda pid: (-totals[pid], pid))
    return ranked.index(position_id) + 1 if position_id in ranked else len(ranked) + 1

def _category_rows(db: Session, category: str):
    """Return paid listings ranked within one category."""
    totals = paid_totals(db)
    positions = db.query(Position).filter(Position.category == category).all()
    return sorted([p for p in positions if p.id in totals],
                  key=lambda p: (-totals[p.id], p.id))

# ─────────────────────────────────────────────────────────────
# app
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="SmartSeek API", version="0.2.0")
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

@app.get("/success")
def success():
    return FileResponse(WEB_DIR / "success.html", media_type="text/html")

@app.get("/cancel")
def cancel():
    return FileResponse(WEB_DIR / "cancel.html", media_type="text/html")

@app.get("/about")
def about():
    return FileResponse(WEB_DIR / "about.html", media_type="text/html")

@app.get("/rules")
def rules():
    return FileResponse(WEB_DIR / "rules.html", media_type="text/html")

@app.get("/advertise")
def advertise():
    return FileResponse(WEB_DIR / "advertise.html", media_type="text/html")

@app.get("/contact")
def contact():
    return FileResponse(WEB_DIR / "contact.html", media_type="text/html")

@app.get("/api/config")
def config():
    return {
        "demo": DEMO_MODE,
        "stripe_live": bool(STRIPE_SECRET),
        "ga4": GA4_MEASUREMENT_ID,
        "public_base_url": PUBLIC_BASE_URL,
        "max_bid": MAX_BID,
        "categories": CATEGORY_CATALOG,
    }

@app.get("/api/categories")
def categories():
    return {"items": CATEGORY_CATALOG, "max_bid": MAX_BID}

# ─────────────────────────────────────────────────────────────
# positions
# ─────────────────────────────────────────────────────────────
@app.get("/api/positions")
def list_positions(
    category: str = "",
    q: str = "",
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    category_match = None
    if category:
        category_match = next((x for x in CATEGORY_CATALOG if x["slug"] == category or x["name"].lower() == category.lower()), None)
        if not category_match:
            raise HTTPException(404, "Category not found")
        category = category_match["name"]
    qset = db.query(Position)
    if category:
        qset = qset.filter(Position.category == category)
    if q:
        like = f"%{q}%"
        qset = qset.filter(Position.name.ilike(like) | Position.domain.ilike(like))
    all_positions = qset.all()
    totals = paid_totals(db)
    out = []
    for p in all_positions:
        paid = int(totals.get(p.id, 0))
        # A listing shell created for an unpaid Checkout is not public yet.
        # Keep the board honest: only completed payments enter any leaderboard.
        if paid <= 0:
            continue
        clicks = db.query(Click).filter(Click.position_id == p.id).count()
        out.append({
            "id": p.id, "slug": p.slug, "name": p.name, "domain": p.domain,
            "description": p.description, "category": p.category, "color": p.color,
            "is_demo": p.is_demo, "created_at": p.created_at.isoformat(),
            "total_paid": paid, "clicks": clicks,
            "category_rank": rank_for_category(db, p.id, paid),
        })
    out.sort(key=lambda x: (-x["total_paid"], x["id"]))
    total = len(out)
    start = (page - 1) * limit
    categories_present = sorted({x["category"] for x in out})
    return {"items": out[start:start + limit], "total": total, "page": page, "limit": limit,
            "categories": CATEGORY_CATALOG, "categories_present": categories_present,
            "max_bid": MAX_BID}

@app.get("/api/leaderboards/{category}")
def category_leaderboard(category: str, page: int = Query(1, ge=1),
                         limit: int = Query(20, ge=1, le=100),
                         db: Session = Depends(get_db)):
    match = next((x for x in CATEGORY_CATALOG if x["slug"] == category or x["name"].lower() == category.lower()), None)
    if not match:
        raise HTTPException(404, "Category not found")
    positions = _category_rows(db, match["name"])
    totals = paid_totals(db)
    start = (page - 1) * limit
    return {"category": match, "items": [
        {"rank": start + i + 1, "id": p.id, "slug": p.slug,
         "name": p.name, "domain": p.domain, "description": p.description,
         "total_paid": totals[p.id],
         "clicks": db.query(Click).filter(Click.position_id == p.id).count()}
        for i, p in enumerate(positions[start:start + limit])
    ], "total": len(positions), "page": page, "limit": limit, "max_bid": MAX_BID}

@app.post("/api/positions")
def create_position(body: PositionIn, db: Session = Depends(get_db)):
    """Create a listing shell; money is recorded only after payment succeeds."""
    s = slugify(body.domain)
    if body.category not in ALLOWED_CATEGORIES:
        raise HTTPException(400, "Choose a valid category")
    existing = db.query(Position).filter(Position.slug == s).first()
    if existing:
        return {"ok": True, "id": existing.id, "slug": existing.slug, "existing": True}
    p = Position(slug=s, name=body.name, domain=body.domain,
                 description=body.description, category=body.category,
                 color=body.color, is_demo=False)
    db.add(p); db.commit()
    return {"ok": True, "id": p.id, "slug": p.slug, "existing": False}

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
    if body.amount > MAX_BID:
        raise HTTPException(400, f"Maximum bid is ${MAX_BID:,}")
    current = total_paid(db, p.id)
    need = current + 1
    if body.amount < need:
        raise HTTPException(400, f"Must be at least ${need} to rank here")
    delta = body.amount - current
    # Never rank a live bid before Stripe confirms it. This also makes
    # cancelled checkouts invisible to the public leaderboard.
    b = Bid(position_id=p.id, amount=delta, status="pending",
            created_at=datetime.now(timezone.utc))
    db.add(b); db.commit()
    proposed_total = current + delta
    rank = rank_for(db, p.id, proposed_total)

    if STRIPE_SECRET:
        base = PUBLIC_BASE_URL.rstrip("/")
        try:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd", "unit_amount": delta * 100,
                        "product_data": {"name": f"Rank #{rank} — {p.name} (total ${proposed_total})"},
                    }, "quantity": 1,
                }], mode="payment",
                success_url=f"{base}/success?bid_id={b.id}",
                cancel_url=f"{base}/cancel?position_id={p.id}",
                metadata={"bid_id": str(b.id), "position_id": str(p.id)},
            )
        except Exception:
            db.delete(b); db.commit()
            raise HTTPException(502, "Stripe checkout could not be created")
        b.stripe_pid = session.id; db.commit()
        return {"checkout_url": session.url, "demo": False, "bid_id": b.id,
                "rank": rank, "total_paid": current, "position_id": p.id, "delta": delta,
                "pending": True}
    b.status = "paid"; b.paid_at = datetime.now(timezone.utc)
    db.add(TradeLog(position_id=p.id, delta=delta, note="payment confirmed (demo)"))
    db.commit()
    new_total = total_paid(db, p.id)
    return {"checkout_url": None, "demo": True, "bid_id": b.id,
            "rank": rank_for(db, p.id), "total_paid": new_total,
            "position_id": p.id, "delta": delta}

@app.post("/api/claim")
def claim_top(body: ClaimIn, db: Session = Depends(get_db)):
    """
    Claim №1: pay enough to beat the current leader.
    Returns a Stripe checkout session when STRIPE_SECRET is set,
    otherwise a demo placeholder.
    """
    s = slugify(body.domain)
    if body.category not in ALLOWED_CATEGORIES:
        raise HTTPException(400, "Choose a valid category")
    if body.amount > MAX_BID:
        raise HTTPException(400, f"Maximum bid is ${MAX_BID:,}")
    existing = db.query(Position).filter(Position.slug == s).first()
    if existing:
        p = existing
    else:
        p = Position(slug=s, name=body.name, domain=body.domain,
                     description=body.description, category=body.category,
                     color=body.color, is_demo=False)
        db.add(p); db.flush()

    totals = paid_totals(db)
    leader_total = max(totals.values(), default=0)
    current = total_paid(db, p.id)
    if not existing:
        # The new position is not ranked until this checkout is paid.
        leader_total = max(leader_total, 0)
    need = max(leader_total + 1, body.amount, current + 1)
    if need > MAX_BID:
        raise HTTPException(400, f"The $${MAX_BID:,} board limit has been reached")
    delta = need - current

    b = Bid(position_id=p.id, amount=delta, status="pending",
            created_at=datetime.now(timezone.utc))
    db.add(b); db.commit()
    rank = rank_for(db, p.id, need)

    if STRIPE_SECRET:
        base = PUBLIC_BASE_URL.rstrip("/")
        try:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd", "unit_amount": delta * 100,
                        "product_data": {"name": f"Rank #1 — {p.name} (total ${need})"},
                    }, "quantity": 1,
                }], mode="payment",
                success_url=f"{base}/success?bid_id={b.id}",
                cancel_url=f"{base}/cancel?position_id={p.id}",
                metadata={"bid_id": str(b.id), "position_id": str(p.id)},
            )
        except Exception:
            db.delete(b); db.commit()
            raise HTTPException(502, "Stripe checkout could not be created")
        b.stripe_pid = session.id; db.commit()
        return {"checkout_url": session.url, "demo": False, "bid_id": b.id,
                "rank": rank, "total_paid": current, "position_id": p.id,
                "delta": delta, "pending": True}
    b.status = "paid"; b.paid_at = datetime.now(timezone.utc)
    db.add(TradeLog(position_id=p.id, delta=delta, note="payment confirmed (demo)"))
    db.commit()
    return {"checkout_url": None, "demo": True, "bid_id": b.id,
            "rank": rank_for(db, p.id), "amount": need,
            "total_paid": total_paid(db, p.id), "position_id": p.id, "delta": delta}

# ─────────────────────────────────────────────────────────────
# stripe webhook — real payments land here
# ─────────────────────────────────────────────────────────────
from fastapi import Request

@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    whsec = STRIPE_WEBHOOK_SECRET
    event = None
    if whsec and sig:
        try:
            event = stripe.Webhook.construct_event(payload, sig, whsec)
        except Exception:
            raise HTTPException(400, "Invalid signature")
    else:
        import json as _json
        try:
            event = _json.loads(payload)
        except Exception:
            raise HTTPException(400, "Bad payload")

    if event.get("type") in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        obj = event["data"]["object"]
        meta = obj.get("metadata") or {}
        bid_id = int(meta.get("bid_id", 0) or 0)
        b = db.query(Bid).filter(Bid.id == bid_id).first() if bid_id else None
        if b and b.status != "paid":
            # Only a completed Checkout can move a bid into the public board.
            b.status = "paid"
            b.paid_at = datetime.now(timezone.utc)
            b.stripe_pid = obj.get("id") or b.stripe_pid
            db.add(TradeLog(position_id=b.position_id, delta=b.amount,
                            note="payment confirmed (stripe)"))
            db.commit()
    return {"received": True}

@app.post("/api/bid/{bid_id}/confirm")
def confirm_bid_demo(bid_id: int, db: Session = Depends(get_db)):
    if not DEMO_MODE:
        raise HTTPException(403, "Manual confirmation is disabled when Stripe is live")
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
    rank = rank_for(db, b.position_id)
    return {"ok": True, "rank": rank, "total_paid": new_total, "position_id": b.position_id}

# ─────────────────────────────────────────────────────────────
# click redirect — honest counting + real destination
# ─────────────────────────────────────────────────────────────
@app.get("/r/{slug}")
def redirect_slug(slug: str, request: Request, db: Session = Depends(get_db)):
    p = db.query(Position).filter(Position.slug == slug).first()
    if not p:
        raise HTTPException(404, "Not found")
    c = Click(
        position_id=p.id,
        source=request.headers.get("x-forwarded-for", "direct").split(",")[0].strip() or "direct",
        country=request.headers.get("cf-ipcountry", "--"),
        referer=request.headers.get("referer", ""),
    )
    db.add(c); db.commit()
    dest = p.domain if p.domain.startswith("http") else f"https://{p.domain}"
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
# analytics + platform earnings
# ─────────────────────────────────────────────────────────────
@app.get("/api/analytics/summary")
def analytics_summary(db: Session = Depends(get_db)):
    total_positions = db.query(Position).count()
    total_clicks = db.query(Click).count()
    pot = db.query(func.coalesce(func.sum(Bid.amount), 0)).filter(Bid.status == "paid").scalar() or 0
    seats_today = db.query(Position).filter(
        Position.created_at >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
    ).count()
    platform_earnings = int(pot * 0.03)
    return {
        "positions": total_positions,
        "clicks": total_clicks,
        "pot": pot,
        "platform_earnings": platform_earnings,
        "seats_today": seats_today,
        "max_bid": MAX_BID,
        "categories": len(CATEGORY_CATALOG),
    }

@app.get("/api/data-health")
def data_health(db: Session = Depends(get_db)):
    """Small, honest health payload for the public data-status indicator."""
    paid = db.query(Bid).filter(Bid.status == "paid").count()
    pending = db.query(Bid).filter(Bid.status == "pending").count()
    categories = sorted({p.category for p in db.query(Position).all()})
    return {
        "status": "live",
        "database": "postgres" if not DATABASE_URL.startswith("sqlite") else "sqlite",
        "positions": db.query(Position).count(),
        "paid_bids": paid,
        "pending_bids": pending,
        "categories_present": categories,
        "max_bid": MAX_BID,
        "stripe": bool(STRIPE_SECRET),
    }

@app.get("/api/analytics/positions/{position_id}")
def position_analytics(position_id: int, db: Session = Depends(get_db)):
    p = db.query(Position).filter(Position.id == position_id).first()
    if not p:
        raise HTTPException(404, "Not found")
    clicks = db.query(Click).filter(Click.position_id == position_id).count()
    paid = total_paid(db, position_id)
    return {
        "position_id": position_id,
        "name": p.name,
        "domain": p.domain,
        "total_paid": paid,
        "clicks": clicks,
        "category": p.category,
        "color": p.color,
    }

@app.get("/api/activity")
def activity(limit: int = Query(14, le=50), db: Session = Depends(get_db)):
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
# seed / reset
# ─────────────────────────────────────────────────────────────
@app.post("/api/seed")
def seed_demo(db: Session = Depends(get_db)):
    """Development helper: seed only the three verified launch players."""
    if db.query(Position).count() > 0:
        return {"ok": True, "message": "board already has listings"}
    launch = [
        ("smartseek", "SmartSeek", "smartseek.com", "The pay-to-rank marketplace for intelligent things.", "Marketing", "#2563EB", 1),
        ("ortaq", "Ortaq", "ortaq.biz", "AI agents that work alongside your team.", "AI Agents", "#0F172A", 3),
        ("ihalezeka", "İhaleZeka", "ihalezeka.com", "AI-powered tender intelligence.", "Data & Research", "#2563EB", 5),
    ]
    for slug, name, domain, desc, cat, color, bid in launch:
        p = Position(slug=slug, name=name, domain=domain, description=desc,
                     category=cat, color=color, is_demo=False)
        db.add(p); db.flush()
        db.add(Bid(position_id=p.id, amount=bid, status="paid",
                   paid_at=datetime.now(timezone.utc)))
    db.commit()
    return {"ok": True, "seeded": len(launch)}

@app.post("/api/reset")
def reset_and_seed(db: Session = Depends(get_db)):
    if not DEMO_MODE:
        raise HTTPException(403, "Reset disabled outside demo mode")
    db.query(TradeLog).delete()
    db.query(Click).delete()
    db.query(Bid).delete()
    db.query(Position).delete()
    db.query(AppSetting).delete()
    db.commit()
    return seed_demo(db)

# ─────────────────────────────────────────────────────────────
# Front-end serving
# ─────────────────────────────────────────────────────────────
from pathlib import Path as _Path
FRONTEND_DIR = _Path(__file__).resolve().parent / "web"

@app.get("/{full_path:path}")
def serve_frontend(request: Request, full_path: str):
    rel = full_path.lstrip("/")
    if rel in {"", "index.html"}:
        return FileResponse(FRONTEND_DIR / "index.html", media_type="text/html")
    target = FRONTEND_DIR / rel
    if target.is_file() and target.suffix in {".html",".js",".css",".png",".svg",".ico",".json",".txt",".xml",".map"}:
        return FileResponse(target)
    return FileResponse(FRONTEND_DIR / "index.html", media_type="text/html")

# ─────────────────────────────────────────────────────────────
# Auto-seed real starter listings on first boot (board never empty)
# ─────────────────────────────────────────────────────────────
def _auto_seed():
    db = SessionLocal()
    try:
        # One-time launch board: only the three verified SmartSeek ecosystem
        # players are seeded. Future bidders can add themselves normally.
        launch_key = db.query(AppSetting).filter(AppSetting.key == "launch_board_v2").first()
        if launch_key:
            # Keep the three house launch entries in the visible category
            # taxonomy even after a restart or a SQLite redeploy.
            category_map = {
                "smartseek": "Marketing",
                "ortaq": "AI Agents",
                "ihalezeka": "Data & Research",
            }
            for slug, category in category_map.items():
                p = db.query(Position).filter(Position.slug == slug).first()
                if p and p.category != category:
                    p.category = category
            db.commit()
            return
        # Remove only the old auto-seeded starter board. Never touch a board
        # that already contains a visitor-created listing or payment.
        old = db.query(Position).all()
        legacy_slugs = {"openai", "anthropic", "perplexity", "github", "huggingface", "ihalezeka", "luxrentals", "7stories", "ortaq", "smartseek"}
        if old and all(p.slug in legacy_slugs for p in old):
            db.query(TradeLog).delete(); db.query(Click).delete(); db.query(Bid).delete(); db.query(Position).delete()
        elif old:
            return
        starters = [
            ("smartseek", "SmartSeek", "smartseek.com", "The pay-to-rank marketplace for intelligent things.", "Marketing", "#2563EB", 1),
            ("ortaq", "Ortaq", "ortaq.biz", "AI agents that work alongside your team.", "AI Agents", "#0F172A", 3),
            ("ihalezeka", "İhaleZeka", "ihalezeka.com", "AI-powered tender intelligence.", "Data & Research", "#2563EB", 5),
        ]
        for slug, name, domain, desc, cat, color, bid in starters:
            p = Position(slug=slug, name=name, domain=domain, description=desc,
                         category=cat, color=color, is_demo=False)
            db.add(p); db.flush()
            db.add(Bid(position_id=p.id, amount=bid, status="paid", paid_at=datetime.now(timezone.utc)))
        db.add(AppSetting(key="launch_board_v2", value="smartseek-ortaq-ihalezeka"))
        db.commit()
    finally:
        db.close()

try:
    Base.metadata.create_all(bind=engine)
    _auto_seed()
except Exception as e:
    print("startup seed warning:", e)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
