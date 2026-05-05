from django.db import connection, OperationalError
from django.db.migrations.executor import MigrationExecutor
from ninja import Router, Schema
from ninja.throttling import AnonRateThrottle

from .models import AppMessage

config_router = Router(tags=["config"])
health_router = Router(tags=["health"])

_health_throttle = AnonRateThrottle(rate="10/s")


class AppMessageSchema(Schema):
    level: str
    message: str


@config_router.get("/app-message", response=AppMessageSchema | None)
def app_message(request):
    return AppMessage.objects.filter(active=True).first()


class HealthSchema(Schema):
    status: str
    db: str
    migrations: str


@health_router.get("/", response={200: HealthSchema, 503: HealthSchema}, throttle=_health_throttle)
def health(request):
    db_status = "ok"
    migrations_status = "ok"

    try:
        connection.ensure_connection()
    except OperationalError:
        db_status = "unavailable"

    if db_status == "ok":
        try:
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
            if plan:
                migrations_status = "pending"
        except Exception:
            migrations_status = "unknown"

    ok = db_status == "ok" and migrations_status == "ok"
    payload = HealthSchema(status="ok" if ok else "degraded", db=db_status, migrations=migrations_status)
    return (200 if ok else 503), payload
