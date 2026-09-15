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

    sms_gateway_url: str = "http://127.0.0.1:8080"
    sms_gateway_user: str = ""
    sms_gateway_password: str = ""
    # local | private | cloud  (API path differs; see gateway.SmsGatewayClient)
    sms_gateway_mode: str = "local"
    sms_sim_number: int = 2

    webhook_base: str = "http://127.0.0.1:8787"
    saily_phone_e164: str = "+17794362345"

    timezone: str = "America/New_York"
    work_windows: str = "09:00-12:00,14:00-18:00"
    min_send_interval_seconds: int = 90
    wa_min_send_interval_seconds: int = 120
    wa_daily_send_limit: int = 30
    wa_job_lease_seconds: int = 180
    daily_plan_hour: int = 8
    daily_plan_minute: int = 30
    scheduler_poll_seconds: int = 20
    device_heartbeat_stale_minutes: int = 45
    # After an uncertain send (timeout / unconfirmed), block Pending re-runs this long.
    outbound_uncertain_cooldown_seconds: int = 3600

    reply_webhook_url: str = ""
    reply_webhook_token: str = ""
    wa_api_token: str = ""

    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    gmail_user: str = ""
    gmail_poll_seconds: int = 30
    # Comma-separated; empty = built-in public mailbox list
    email_public_domains: str = ""
    # Same Gmail thread but From domain ≠ outbound: still accept replies (flag unexpectedSender)
    email_allow_cross_domain_thread_reply: bool = True

    data_dir: str = ""

    def resolved_data_dir(self) -> Path:
        if self.data_dir:
            p = Path(self.data_dir)
        else:
            p = Path(__file__).resolve().parents[1] / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
