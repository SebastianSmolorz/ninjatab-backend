from django.conf import settings as django_settings
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class MagicLinkSchema(BaseModel):
    email: EmailStr
    skip_email: bool = False
    platform: str | None = None


class MagicLinkSuccessSchema(BaseModel):
    success: bool
    magic_url: str | None = None


class VerifyMagicLinkSchema(BaseModel):
    token: str


class AuthUserSchema(BaseModel):
    id: str
    email: str
    first_name: str
    last_name: str
    analytics_opted_in: bool
    minimum_app_version: str

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode='before')
    @classmethod
    def extract_uuid(cls, data):
        if hasattr(data, 'uuid'):
            return {
                'id': str(data.uuid),
                'email': data.email,
                'first_name': data.first_name,
                'last_name': data.last_name,
                'analytics_opted_in': data.analytics_opted_in,
                'minimum_app_version': django_settings.MINIMUM_APP_VERSION,
            }
        return data


class TokenResponseSchema(BaseModel):
    user: AuthUserSchema
    is_new: bool = False


class RefreshResponseSchema(BaseModel):
    success: bool


class LogoutResponseSchema(BaseModel):
    success: bool


class UpdateProfileSchema(BaseModel):
    first_name: str
    analytics_opted_in: bool | None = None


class SocialLoginSchema(BaseModel):
    provider: str
    id_token: str
    first_name: str | None = None
    last_name: str | None = None
    platform: str | None = None


class PaymentMethodSchema(BaseModel):
    provider: str
    username: str
    is_preferred: bool

    model_config = ConfigDict(from_attributes=True)


class PaymentMethodUpsertSchema(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    is_preferred: bool = False
