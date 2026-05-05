from ninja import Router, Schema

from .models import AppMessage

config_router = Router(tags=["config"])


class AppMessageSchema(Schema):
    level: str
    message: str


@config_router.get("/app-message", response=AppMessageSchema | None)
def app_message(request):
    return AppMessage.objects.filter(active=True).first()
