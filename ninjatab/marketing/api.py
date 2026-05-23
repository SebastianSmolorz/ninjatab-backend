from ninja import Router
from ninja.errors import HttpError
from ninjatab.utilities.analytics import safe_capture
from ninjatab.marketing.models import WaitlistEntry, WaitlistPageView
from ninjatab.marketing.schemas import WaitlistCreateSchema, WaitlistResponseSchema, AppInstallSchema, QRCodeScannedSchema, DownloadClickSchema

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

    safe_capture("$anon", "waitlist_joined", properties={"platform": payload.platform})

    return {"success": True}


@marketing_router.post("/install", response=WaitlistResponseSchema)
def app_install(request, payload: AppInstallSchema):
    safe_capture("$anon", "app_installed", properties={"platform": payload.platform})
    return {"success": True}


@marketing_router.post("/download-click", response=WaitlistResponseSchema)
def download_click(request, payload: DownloadClickSchema):
    properties = {k: v for k, v in payload.model_dump().items() if v is not None}
    properties["platform"] = payload.platform.value
    safe_capture("$anon", "download_link_clicked", properties=properties)
    return {"success": True}


@marketing_router.post("/qr-scanned", response=WaitlistResponseSchema)
def qr_code_scanned(request, payload: QRCodeScannedSchema):
    safe_capture("$anon", "qr_code_scanned", properties={
        "qr_id": payload.qr_id,
        "utm_campaign": payload.utm_campaign,
        "utm_medium": payload.utm_medium,
        "utm_source": payload.utm_source,
    })
    return {"success": True}
