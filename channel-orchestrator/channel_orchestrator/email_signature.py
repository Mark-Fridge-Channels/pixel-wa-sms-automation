"""Hommio outbound email signature (appended at Gmail send time)."""

from __future__ import annotations

from html import escape

from .config import settings

SIGNATURE_MARK = "hommio-email-signature"
DEFAULT_LOGO_URL = (
    "https://amzn-s3-fc-bucket.s3.sa-east-1.amazonaws.com/"
    "images/hommio-email-signature-logo.png"
)


def signature_enabled() -> bool:
    return bool(getattr(settings, "email_signature_enabled", True))


def signature_logo_url() -> str:
    url = (getattr(settings, "email_signature_logo_url", "") or "").strip()
    return url or DEFAULT_LOGO_URL


def signature_rebrand_note() -> str:
    return (getattr(settings, "email_signature_rebrand_note", "") or "").strip()


def signature_already_present(text: str | None) -> bool:
    value = text or ""
    return SIGNATURE_MARK in value or "hommio-email-signature-logo.png" in value


def signature_html() -> str:
    logo = escape(signature_logo_url(), quote=True)
    note = signature_rebrand_note()
    # Keep the right column to ~3 lines: identity / pitch / note+site
    footer_bits: list[str] = []
    if note:
        footer_bits.append(
            f'<span style="color:#777777;">{escape(note)}</span>'
        )
    footer_bits.append(
        '<a href="https://hommio.ai" target="_blank" style="color:#111111;text-decoration:underline;">hommio.ai</a>'
    )
    footer = '&nbsp;·&nbsp;'.join(footer_bits)
    return f"""<table cellpadding="0" cellspacing="0" border="0" role="presentation" data-{SIGNATURE_MARK}="1" style="border-collapse:collapse;font-family:Arial,Helvetica,sans-serif;color:#111111;">
  <tr>
    <td valign="middle" style="padding:0 18px 0 0;">
      <a href="https://hommio.ai" target="_blank" style="text-decoration:none;">
        <img src="{logo}" width="120" height="120" alt="hommio" style="display:block;border:0;width:120px;height:120px;"/>
      </a>
    </td>
    <td valign="middle" style="padding:0 0 0 18px;border-left:2px solid #111111;">
      <table cellpadding="0" cellspacing="0" border="0" role="presentation" style="border-collapse:collapse;">
        <tr>
          <td style="font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:700;line-height:1.35;color:#111111;padding:0 0 6px 0;">
            Ella&nbsp;<span style="font-weight:400;color:#555555;">· Account Manager</span>
          </td>
        </tr>
        <tr>
          <td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.45;color:#333333;padding:0 0 8px 0;">
            <span style="font-weight:700;font-style:italic;color:#111111;">Retention is at home</span><br>
            <span style="font-size:12px;color:#666666;">In-home retention solution for CPG brands.</span>
          </td>
        </tr>
        <tr>
          <td style="font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:1.4;color:#777777;padding:0;">
            {footer}
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>"""


def signature_plain() -> str:
    lines = [
        "Ella · Account Manager",
        "Retention is at home",
        "In-home retention solution for CPG brands.",
    ]
    note = signature_rebrand_note()
    if note:
        lines.append(f"{note} · https://hommio.ai")
    else:
        lines.append("https://hommio.ai")
    return "\n".join(lines)


def append_signature_html(html_core: str) -> str:
    """Append trusted signature HTML after a sanitized (or plain-escaped) body."""
    if not signature_enabled():
        return html_core
    if signature_already_present(html_core):
        return html_core
    sig = signature_html()
    core = (html_core or "").rstrip()
    if core:
        return f"{core}<br><br>{sig}"
    return sig


def plain_body_to_html(text: str) -> str:
    return escape(text or "").replace("\n", "<br>\n")
