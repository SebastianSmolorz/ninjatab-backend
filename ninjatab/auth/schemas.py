from pydantic import BaseModel, ConfigDict, EmailStr, model_validator


class MagicLinkSchema(BaseModel):
    email: EmailStr
    skip_email: bool = False


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
            }
        return data


class TokenResponseSchema(BaseModel):
    user: AuthUserSchema


class RefreshResponseSchema(BaseModel):
    success: bool


class LogoutResponseSchema(BaseModel):
    success: bool


class UpdateProfileSchema(BaseModel):
    first_name: str


class SocialLoginSchema(BaseModel):
    provider: str
    id_token: str
    first_name: str | None = None
    last_name: str | None = None
