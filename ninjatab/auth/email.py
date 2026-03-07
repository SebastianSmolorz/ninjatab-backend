import requests

from django.conf import settings


LOGO_URL = "https://tab.ninja/logo.png"


def send_magic_link(email: str, token: str) -> None:
    magic_url = f"{settings.MAGIC_LINK_BASE_URL}?token={token}"

    if settings.DEBUG:
        print(f"\n[MAGIC LINK] {email}\n{magic_url}\n")
        return

    logo_url = LOGO_URL

    html = f"""\
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="margin: 0; padding: 0; background-color: #f4f4f5; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #f4f4f5; padding: 40px 0;">
        <tr>
          <td align="center">
            <table width="480" cellpadding="0" cellspacing="0" style="background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
              <!-- Logo -->
              <tr>
                <td align="center" style="padding: 32px 40px 16px;">
                  <img src="{logo_url}" alt="Tab Ninja" width="80" style="display: block;" />
                </td>
              </tr>
              <!-- Heading -->
              <tr>
                <td align="center" style="padding: 0 40px 8px;">
                  <h1 style="margin: 0; font-size: 22px; font-weight: 700; color: #111827;">Sign in to Tab Ninja</h1>
                </td>
              </tr>
              <!-- Body text -->
              <tr>
                <td align="center" style="padding: 0 40px 24px;">
                  <p style="margin: 0; font-size: 15px; color: #6b7280; line-height: 1.5;">
                    Tap the button below to securely sign in. No password needed.
                  </p>
                </td>
              </tr>
              <!-- CTA Button -->
              <tr>
                <td align="center" style="padding: 0 40px 24px;">
                  <a href="{magic_url}"
                     style="display: inline-block; padding: 14px 32px; background-color: #111827; color: #ffffff;
                            text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 15px;">
                    Sign in
                  </a>
                </td>
              </tr>
              <!-- Divider -->
              <tr>
                <td style="padding: 0 40px;">
                  <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 0;" />
                </td>
              </tr>
              <!-- Fallback link -->
              <tr>
                <td style="padding: 20px 40px 12px;">
                  <p style="margin: 0; font-size: 13px; color: #9ca3af;">
                    Or copy and paste this link into your browser:
                  </p>
                </td>
              </tr>
              <tr>
                <td style="padding: 0 40px 24px;">
                  <a href="{magic_url}" style="font-size: 13px; color: #6b7280; word-break: break-all; text-decoration: underline;">{magic_url}</a>
                </td>
              </tr>
              <!-- Footer -->
              <tr>
                <td style="padding: 16px 40px 32px;">
                  <p style="margin: 0; font-size: 12px; color: #d1d5db;">
                    This link expires in 15 minutes. If you didn't request this, you can safely ignore this email.
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
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
            "sender": {"name": "Tab Ninja", "email": "seb@tab.ninja"},
            "to": [{"email": email}],
            "subject": "Your Tab Ninja sign-in link",
            "htmlContent": html,
        },
    )
    resp.raise_for_status()
