import json
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import airtable_client as at
import anthropic_client as ai
from config import get_settings
from schemas import (
    ChatIn,
    ChatOut,
    LeadIn,
    ListingIn,
    ListingOut,
    ResellerAnalyzeIn,
    ResellerAnalyzeOut,
    WantToBuyIn,
)

logger = logging.getLogger("food-equipment")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Food Equipment API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"service": "food-equipment-api", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- Leads ----------

@app.post("/leads", status_code=201)
def create_lead(lead: LeadIn):
    notes_lines = [f"Email: {lead.email}"]
    if lead.phone:
        notes_lines.append(f"Phone: {lead.phone}")
    if lead.city:
        notes_lines.append(f"City: {lead.city}")
    if lead.equipment_type:
        notes_lines.append(f"Equipment: {lead.equipment_type}")
    if lead.budget is not None:
        notes_lines.append(f"Budget: {lead.budget}")
    if lead.message:
        notes_lines.append(f"Message: {lead.message}")
    if lead.source:
        notes_lines.append(f"Source: {lead.source}")
    fields = {
        "Name": lead.name,
        "Notes": "\n".join(notes_lines),
    }
    try:
        rec = at.create_record(at.leads_table(), fields)
    except Exception as e:
        logger.exception("airtable lead create failed")
        raise HTTPException(status_code=502, detail=f"Airtable error: {e}")
    return {"id": rec["id"], "fields": rec.get("fields", {})}


# ---------- Listings ----------

def _listing_fields(l: ListingIn) -> dict:
    return {
        "Title": l.title,
        "Description": l.description,
        "Category": l.category,
        "Condition": l.condition,
        "Price": l.price,
        "Currency": l.currency,
        "City": l.city,
        "SellerName": l.seller_name,
        "SellerEmail": l.seller_email,
        "SellerPhone": l.seller_phone,
        "Photos": ",".join(l.photos) if l.photos else None,
        "Year": l.year,
        "Brand": l.brand,
    }


def _record_to_listing(rec: dict) -> ListingOut:
    f = rec.get("fields", {})
    photos_raw = f.get("Photos")
    if isinstance(photos_raw, str):
        photos = [p.strip() for p in photos_raw.split(",") if p.strip()]
    elif isinstance(photos_raw, list):
        photos = photos_raw
    else:
        photos = []
    return ListingOut(
        id=rec["id"],
        title=f.get("Title", ""),
        description=f.get("Description", ""),
        category=f.get("Category", ""),
        condition=f.get("Condition", "used"),
        price=float(f.get("Price") or 0),
        currency=f.get("Currency", "USD"),
        city=f.get("City"),
        seller_name=f.get("SellerName", ""),
        seller_email=f.get("SellerEmail", ""),
        seller_phone=f.get("SellerPhone"),
        photos=photos,
        year=f.get("Year"),
        brand=f.get("Brand"),
        created_at=rec.get("createdTime"),
    )


@app.post("/listings", status_code=201, response_model=ListingOut)
def create_listing(listing: ListingIn):
    try:
        rec = at.create_record(at.listings_table(), _listing_fields(listing))
    except Exception as e:
        logger.exception("airtable listing create failed")
        raise HTTPException(status_code=502, detail=f"Airtable error: {e}")
    return _record_to_listing(rec)


@app.get("/listings", response_model=list[ListingOut])
def list_listings(
    category: Optional[str] = None,
    condition: Optional[str] = None,
    city: Optional[str] = None,
    brand: Optional[str] = None,
    price_min: Optional[float] = Query(None, ge=0),
    price_max: Optional[float] = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    parts: list[str] = []
    if category:
        parts.append(f"{{Category}}='{category}'")
    if condition:
        parts.append(f"{{Condition}}='{condition}'")
    if city:
        parts.append(f"{{City}}='{city}'")
    if brand:
        parts.append(f"{{Brand}}='{brand}'")
    if price_min is not None:
        parts.append(f"{{Price}}>={price_min}")
    if price_max is not None:
        parts.append(f"{{Price}}<={price_max}")
    formula = f"AND({', '.join(parts)})" if parts else None

    try:
        records = at.list_records(
            at.listings_table(),
            formula=formula,
            max_records=limit,
            sort=["-Price"],
        )
    except Exception as e:
        logger.exception("airtable listings query failed")
        raise HTTPException(status_code=502, detail=f"Airtable error: {e}")
    return [_record_to_listing(r) for r in records]


@app.get("/listings/{listing_id}", response_model=ListingOut)
def get_listing(listing_id: str):
    try:
        rec = at.get_record(at.listings_table(), listing_id)
    except Exception as e:
        logger.exception("airtable listing fetch failed")
        raise HTTPException(status_code=404, detail=f"Listing not found: {e}")
    return _record_to_listing(rec)


# ---------- Want to buy ----------

@app.post("/want-to-buy", status_code=201)
def create_want_to_buy(w: WantToBuyIn):
    fields = {
        "Name": w.name,
        "Email": w.email,
        "Phone": w.phone,
        "EquipmentType": w.equipment_type,
        "BudgetMin": w.budget_min,
        "BudgetMax": w.budget_max,
        "City": w.city,
        "Details": w.details,
        "Urgency": w.urgency,
    }
    try:
        rec = at.create_record(at.want_to_buy_table(), fields)
    except Exception as e:
        logger.exception("airtable want-to-buy create failed")
        raise HTTPException(status_code=502, detail=f"Airtable error: {e}")
    return {"id": rec["id"], "fields": rec.get("fields", {})}


# ---------- AI chat ----------

@app.post("/chat", response_model=ChatOut)
def chat(req: ChatIn):
    try:
        text, stop = ai.chat(req.messages, system=req.system, max_tokens=req.max_tokens)
    except Exception as e:
        logger.exception("anthropic chat failed")
        raise HTTPException(status_code=502, detail=f"AI error: {e}")
    return ChatOut(reply=text, stop_reason=stop)


# ---------- Reseller analysis ----------

@app.post("/reseller/analyze", response_model=ResellerAnalyzeOut)
def reseller_analyze(item: ResellerAnalyzeIn):
    try:
        return ai.analyze_for_reseller(item)
    except json.JSONDecodeError as e:
        logger.exception("reseller JSON parse failed")
        raise HTTPException(status_code=502, detail=f"AI returned invalid JSON: {e}")
    except Exception as e:
        logger.exception("reseller analyze failed")
        raise HTTPException(status_code=502, detail=f"AI error: {e}")
