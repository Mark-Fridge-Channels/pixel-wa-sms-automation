from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    notion_token: str = ""
    notion_task_ds: str = "79601071-9560-41c1-9ad2-a036bd4c2df8"
    notion_conversation_ds: str = "0fe672f1-49c6-498b-a4c6-d5c5fe6d3fa4"
    notion_contact_ds: str = "e9fce670-c858-4ab2-9d50-fd196b64f8ed"
    notion_keyperson_ds: str = "cd09166f-d9fd-836c-864b-073b80032dce"
    notion_conversation_db: str = "a7ded397b23f47f1a9113855fc3d10ce"
    # FC3.0-Follow-up-ClientDB (Domain via Client relation)
    notion_followup_client_ds: str = "9a07646d-190c-4346-9ab6-96c2e40d66a7"

    sms_gateway_url: str = "http://127.0.0.1:8080"
    sms_gateway_user: str = ""
    sms_gateway_password: str = ""
    # local | private | cloud  (API path differs; see gateway.SmsGatewayClient)
    sms_gateway_mode: str = "local"
    sms_sim_number: int = 2

    webhook_base: str = "http://127.0.0.1:8787"
    saily_phone_e164: str = "+16208941711"

    timezone: str = "America/New_York"
    # Legacy (daily-plan era). Outbound scan no longer gates on work windows.
    work_windows: str = "09:00-12:00,14:00-18:00"
    min_send_interval_seconds: int = 90
    wa_min_send_interval_seconds: int = 120
    # 0 = disabled (no daily WhatsApp cap)
    wa_daily_send_limit: int = 0
    wa_job_lease_seconds: int = 180
    # Min seconds between WhatsApp number-probe jobs (no daily cap).
    wa_probe_interval_seconds: int = 120
    daily_plan_hour: int = 8
    daily_plan_minute: int = 30
    # Legacy poll used by old daily-plan loop; prefer scheduler_scan_seconds.
    scheduler_poll_seconds: int = 20
    # How often Scanner queries Notion for due Pending tasks.
    scheduler_scan_seconds: int = 600
    # Gap between SMS/WA starts (Email is parallel and ignores this).
    send_gap_seconds: int = 90
    device_heartbeat_stale_minutes: int = 45
    # Ops SMS when WA Companion / phone looks unhealthy (empty = disabled).
    alert_sms_to: str = "+8615810494081"
    alert_sms_cooldown_minutes: int = 60
    # Require this many consecutive unhealthy scheduler checks before SMS (≈ minutes if tick~1s gated).
    alert_unhealthy_grace_minutes: int = 20
    # After an uncertain send (timeout / unconfirmed), block Pending re-runs this long.
    outbound_uncertain_cooldown_seconds: int = 3600

    reply_webhook_url: str = ""
    reply_webhook_token: str = ""
    # Cold inbound (no Task): POST /api/inbound. Empty → derive from reply_webhook_url.
    inbound_webhook_url: str = "https://followup-portal.fridgechannels.com/api/inbound"
    wa_api_token: str = ""
    # Optional dedicated token for /wa/probe*; empty → fall back to wa_api_token.
    # Probe endpoints always require a non-empty configured token.
    wa_probe_api_token: str = ""
    # Optional; falls back to wa_api_token for /api/monitor/settings
    monitor_token: str = ""

    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_user: str = ""
    gmail_poll_seconds: int = 30
    # Comma-separated; empty = built-in public mailbox list
    email_public_domains: str = ""
    # Own / colleague domains excluded from cold-inbound external address set
    email_internal_domains: str = "fridgechannels.com"
    # Same Gmail thread but From domain ≠ outbound: still accept replies (flag unexpectedSender)
    email_allow_cross_domain_thread_reply: bool = True
    # Full-table Follow-up Client → Domain cache refresh interval
    client_domain_sync_seconds: int = 3600

    # S3 for inbound WhatsApp media (image/video/audio/file) → Portal mediaUrl
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    s3_bucket: str = ""
    s3_prefix: str = "wa-inbound"
    # Optional CDN / public base, e.g. https://cdn.example.com — else virtual-hosted S3 URL
    s3_public_base_url: str = ""
    # Email attachments → Portal attachments[] (S3 key prefix files/{id}.ext)
    s3_file_prefix: str = "files"
    email_attachment_mime_types: str = "application/pdf,image/jpeg,image/png,image/webp"
    email_attachment_max_bytes: int = 10 * 1024 * 1024
    email_attachment_max_count: int = 5

    data_dir: str = ""

    def resolved_data_dir(self) -> Path:
        if self.data_dir:
            p = Path(self.data_dir)
        else:
            p = Path(__file__).resolve().parents[1] / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
