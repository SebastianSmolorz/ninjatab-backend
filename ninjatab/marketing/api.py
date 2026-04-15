from ninja import Router
from ninja.errors import HttpError
from posthog import new_context, identify_context, capture as ph_capture

from ninjatab.marketing.models import WaitlistEntry, WaitlistPageView
from ninjatab.marketing.schemas import WaitlistCreateSchema, WaitlistResponseSchema, AppInstallSchema

marketing_router = Router(tags=["marketing"])


@marketing_router.post("/waitlist/pageview", response=WaitlistResponseSchema)
def waitlist_pageview(request):
    WaitlistPageView.objects.create()
    return {"success": True}


@marketing_router.post("/waitlist", response=WaitlistResponseSchema)
def join_waitlist(request, payload: WaitlistCreateSchema):
    if WaitlistEntry.objects.filter(email=payload.email.lower()).exists():
        raise HttpError(409, "This email is already on the waitlist.")
    WaitlistEntry.objects.create(email=payload.email, platform=payload.platform)

    with new_context():
        identify_context("$anon")
        ph_capture("waitlist_joined", properties={"platform": payload.platform})

    return {"success": True}


@marketing_router.post("/install", response=WaitlistResponseSchema)
def app_install(request, payload: AppInstallSchema):
    with new_context():
        identify_context("$anon")
        ph_capture("app_installed", properties={"platform": payload.platform})
    return {"success": True}
