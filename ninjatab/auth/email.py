import requests

from django.conf import settings


def send_magic_link(email: str, token: str) -> None:
    magic_url = f"{settings.MAGIC_LINK_BASE_URL}?token={token}"

    # if settings.DEBUG:
    #     print(f"\n[MAGIC LINK] {email}\n{magic_url}\n")
    #     return

    html = f"""\
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"></head>
        <body style="font-family: sans-serif; max-width: 480px; margin: 0 auto; padding: 24px;">
          <h2 style="color: #111;">NinjaTab</h2>
          <p>Click below to sign in:</p>
          <a href="{magic_url}"
             style="display: inline-block; padding: 12px 24px; background: #111; color: #fff;
                    text-decoration: none; border-radius: 6px; font-weight: 600;">
            Sign in
          </a>
          <p style="margin-top: 24px; font-size: 13px; color: #666;">
            Or copy this link:<br>
            <a href="{magic_url}" style="color: #111; word-break: break-all;">{magic_url}</a>
          </p>
          <p style="font-size: 12px; color: #999;">This link expires in 15 minutes.</p>
        </body>
        </html>
    """

    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": settings.BREVO_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "sender": {"name": "NinjaTab", "email": "seb@tab.ninja"},
            "to": [{"email": email}],
            "subject": "Your NinjaTab sign-in link",
            "htmlContent": html,
        },
    )
    resp.raise_for_status()
