from typing import Optional
from pydantic import BaseModel, EmailStr
from enum import Enum


class Platform(str, Enum):
    android = "android"
    ios = "ios"


class DownloadClickSchema(BaseModel):
    platform: Platform
    location: Optional[str] = None
    page_path: Optional[str] = None
    referrer: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    utm_term: Optional[str] = None
    utm_content: Optional[str] = None
    utm_id: Optional[str] = None
    gclid: Optional[str] = None
    fbclid: Optional[str] = None


class WaitlistCreateSchema(BaseModel):
    email: EmailStr
    platform: Platform


class WaitlistResponseSchema(BaseModel):
    success: bool


class AppInstallSchema(BaseModel):
    platform: Platform


class QRCodeScannedSchema(BaseModel):
    qr_id: str
    utm_campaign: str
    utm_medium: str
    utm_source: str
