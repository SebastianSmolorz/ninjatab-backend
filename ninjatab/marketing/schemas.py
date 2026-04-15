from pydantic import BaseModel, EmailStr
from enum import Enum


class Platform(str, Enum):
    android = "android"
    ios = "ios"


class WaitlistCreateSchema(BaseModel):
    email: EmailStr
    platform: Platform


class AppInstallSchema(BaseModel):
    platform: Platform


class WaitlistResponseSchema(BaseModel):
    success: bool
