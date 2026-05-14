from pydantic import BaseModel, EmailStr
from enum import Enum


class Platform(str, Enum):
    android = "android"
    ios = "ios"


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
