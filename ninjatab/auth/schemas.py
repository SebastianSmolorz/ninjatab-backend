from pydantic import BaseModel, EmailStr


class LoginSchema(BaseModel):
    email: EmailStr


class AuthUserSchema(BaseModel):
    id: int
    email: str
    first_name: str
    last_name: str

    class Config:
        from_attributes = True


class TokenResponseSchema(BaseModel):
    access_token: str
    refresh_token: str
    user: AuthUserSchema


class RefreshSchema(BaseModel):
    refresh_token: str


class RefreshResponseSchema(BaseModel):
    access_token: str
