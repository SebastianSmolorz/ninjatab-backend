from typing import Any
from pydantic import BaseModel, EmailStr, model_validator


class MagicLinkSchema(BaseModel):
    email: EmailStr


class MagicLinkSuccessSchema(BaseModel):
    success: bool


class VerifyMagicLinkSchema(BaseModel):
    token: str


class AuthUserSchema(BaseModel):
    id: str
    email: str
    first_name: str
    last_name: str

    class Config:
        from_attributes = True

    @model_validator(mode='before')
    @classmethod
    def extract_uuid(cls, data):
        if hasattr(data, 'uuid'):
            return {
                'id': str(data.uuid),
                'email': data.email,
                'first_name': data.first_name,
                'last_name': data.last_name,
            }
        return data


class TokenResponseSchema(BaseModel):
    access_token: str
    refresh_token: str
    user: AuthUserSchema


class RefreshSchema(BaseModel):
    refresh_token: str


class RefreshResponseSchema(BaseModel):
    access_token: str
