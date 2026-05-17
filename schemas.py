from typing import Literal, Optional
from pydantic import BaseModel, EmailStr, Field


class LeadIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=50)
    equipment_type: Optional[str] = Field(None, max_length=200)
    budget: Optional[float] = Field(None, ge=0)
    city: Optional[str] = Field(None, max_length=100)
    message: Optional[str] = Field(None, max_length=2000)
    source: Optional[str] = Field(None, max_length=100)
    chat: Optional[str] = Field(None, max_length=50000)


class ListingIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., max_length=5000)
    category: str = Field(..., max_length=100)
    condition: Literal["new", "used", "refurbished"] = "used"
    price: float = Field(..., ge=0)
    currency: str = Field("USD", max_length=3)
    city: Optional[str] = Field(None, max_length=100)
    seller_name: str = Field(..., max_length=200)
    seller_email: EmailStr
    seller_phone: Optional[str] = Field(None, max_length=50)
    photos: list[str] = Field(default_factory=list)
    year: Optional[int] = Field(None, ge=1900, le=2100)
    brand: Optional[str] = Field(None, max_length=100)


class WantToBuyIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: EmailStr
    phone: Optional[str] = Field(None, max_length=50)
    equipment_type: str = Field(..., max_length=200)
    budget_min: Optional[float] = Field(None, ge=0)
    budget_max: Optional[float] = Field(None, ge=0)
    city: Optional[str] = Field(None, max_length=100)
    details: Optional[str] = Field(None, max_length=2000)
    urgency: Optional[Literal["low", "medium", "high"]] = "medium"


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    system: Optional[str] = None
    max_tokens: int = Field(1024, ge=1, le=8192)


class ChatOut(BaseModel):
    reply: str
    stop_reason: Optional[str] = None


class ResellerAnalyzeIn(BaseModel):
    title: str = Field(..., max_length=200)
    description: Optional[str] = Field(None, max_length=5000)
    category: str = Field(..., max_length=100)
    condition: Literal["new", "used", "refurbished"] = "used"
    year: Optional[int] = Field(None, ge=1900, le=2100)
    brand: Optional[str] = Field(None, max_length=100)
    asking_price: float = Field(..., ge=0)
    currency: str = Field("USD", max_length=3)
    city: Optional[str] = Field(None, max_length=100)
    photos: list[str] = Field(default_factory=list)


class ResellerAnalyzeOut(BaseModel):
    recommended_buy_price: float
    estimated_resale_price: float
    estimated_margin: float
    margin_percent: float
    confidence: Literal["low", "medium", "high"]
    rationale: str
    risks: list[str]
    suggested_actions: list[str]


class FetchUrlIn(BaseModel):
    url: str = Field(..., max_length=2000)


class FetchUrlOut(BaseModel):
    text: str


class ListingOut(BaseModel):
    id: str
    title: str
    description: str
    category: str
    condition: str
    price: float
    currency: str
    city: Optional[str] = None
    seller_name: str
    seller_email: str
    seller_phone: Optional[str] = None
    photos: list[str] = Field(default_factory=list)
    year: Optional[int] = None
    brand: Optional[str] = None
    created_at: Optional[str] = None
