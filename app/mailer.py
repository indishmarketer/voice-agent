"""Transactional email via Brevo's HTTP API.

A plain POST with httpx is simpler and more reliable from an async app than
raw SMTP - no connection pooling or TLS handshake to manage ourselves. Used
for exactly one thing right now: magic-link admin login.
"""
import httpx

from . import config

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"


async def send_login_link(to_email: str, link: str) -> None:
    if not config.BREVO_API_KEY or not config.BREVO_SENDER_EMAIL:
        raise RuntimeError("Brevo is not configured (BREVO_API_KEY / BREVO_SENDER_EMAIL)")

    payload = {
        "sender": {"name": config.BREVO_SENDER_NAME, "email": config.BREVO_SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": f"Sign in to {config.BRAND_NAME} admin",
        "htmlContent": (
            "<p>Click below to sign in to the admin dashboard. This link "
            "works once and expires in 15 minutes.</p>"
            f'<p><a href="{link}">{link}</a></p>'
            "<p>If you did not request this, you can ignore this email.</p>"
        ),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            BREVO_SEND_URL,
            headers={
                "api-key": config.BREVO_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
        )
    response.raise_for_status()
