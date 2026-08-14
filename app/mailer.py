"""Transactional email via Brevo's HTTP API.

A plain POST with httpx is simpler and more reliable from an async app than
raw SMTP - no connection pooling or TLS handshake to manage ourselves. Used
for exactly one thing right now: magic-link admin login.
"""
import httpx

from . import config

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"


async def _send(to_email: str, subject: str, html: str) -> None:
    if not config.BREVO_API_KEY or not config.BREVO_SENDER_EMAIL:
        raise RuntimeError("Brevo is not configured (BREVO_API_KEY / BREVO_SENDER_EMAIL)")

    payload = {
        "sender": {"name": config.BREVO_SENDER_NAME, "email": config.BREVO_SENDER_EMAIL},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html,
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


async def send_login_link(to_email: str, link: str) -> None:
    await _send(
        to_email,
        f"Sign in to {config.BRAND_NAME} admin",
        "<p>Click below to sign in to the admin dashboard. This link "
        "works once and expires in 15 minutes.</p>"
        f'<p><a href="{link}">{link}</a></p>'
        "<p>If you did not request this, you can ignore this email.</p>",
    )


async def send_account_approved(to_email: str, business_name: str, login_link: str) -> None:
    await _send(
        to_email,
        f"You're approved - {business_name} is live on {config.BRAND_NAME}",
        f"<p>Good news - <strong>{business_name}</strong> has been approved for "
        f"the {config.BRAND_NAME} voice agent trial.</p>"
        f'<p><a href="{login_link}">Click here to sign in to your dashboard</a> '
        "and upload your knowledge base, then grab your embed code or share "
        "link.</p>"
        "<p>This link signs you in directly - no password needed.</p>",
    )


async def send_email_change_confirmation(new_email: str, business_name: str,
                                         confirm_link: str) -> None:
    await _send(
        new_email,
        f"Confirm your new login email for {business_name}",
        "<p>You asked to change the email used to sign in to your "
        f"{config.BRAND_NAME} dashboard.</p>"
        f'<p><a href="{confirm_link}">Click here to confirm this email address</a> '
        "- this link expires in 15 minutes.</p>"
        "<p>Nothing changes until you click that link. If you did not request "
        "this, you can safely ignore this email.</p>",
    )


async def send_limit_reached(to_email: str, business_name: str, reason: str) -> None:
    if reason == "trial_expired":
        subject = f"{business_name}: your free trial has ended"
        body = ("<p>Your free trial on the voice agent has ended. Reply to "
                "this email to subscribe and keep it running.</p>")
    elif reason == "account_monthly_cap":
        subject = f"{business_name}: this month's fair-use limit reached"
        body = ("<p>Your voice agent has hit this month's fair-use minutes "
                "limit on your subscription. It resets next month, or reply "
                "to this email if you need a higher limit.</p>")
    else:
        subject = f"{business_name}: today's free minutes are used up"
        body = ("<p>Your voice agent hit today's free usage limit. It will "
                "reset tomorrow, or reply to this email to subscribe for "
                "unlimited access now.</p>")
    await _send(to_email, subject, body)
