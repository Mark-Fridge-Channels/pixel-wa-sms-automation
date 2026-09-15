from __future__ import annotations

import argparse
import json
import logging
import time
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import uvicorn

from .config import settings
from .gateway import SmsGatewayClient
from .inbound import poll_gmail_inbound
from .outbound import process_due
from .scheduler import build_daily_plan, load_plan, normalize_channel, ny_today
from .server import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cli")


def register_webhook(url: str, event: str = "sms:received") -> dict:
    client = SmsGatewayClient()
    payload = {"id": str(uuid.uuid4()), "url": url, "event": event}
    with httpx.Client(timeout=30.0, auth=client.auth) as http:
        r = http.post(f"{client.base}/webhooks", json=payload)
        print("register status", r.status_code, r.text[:500])
        r.raise_for_status()
        return r.json() if r.content else {"status_code": r.status_code}


def list_webhooks() -> list:
    client = SmsGatewayClient()
    with httpx.Client(timeout=30.0, auth=client.auth) as http:
        r = http.get(f"{client.base}/webhooks")
        r.raise_for_status()
        return r.json()


def main() -> None:
    parser = argparse.ArgumentParser(prog="channel_orchestrator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="Ping SMS gateway")

    p_send = sub.add_parser("send", help="Send one SMS via gateway")
    p_send.add_argument("--to", required=True)
    p_send.add_argument("--text", required=True)

    p_serve = sub.add_parser("serve", help="Run webhook + WA job API server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8787)

    p_wh = sub.add_parser("webhook-register", help="Register sms:received webhook on gateway")
    p_wh.add_argument("--url", default="http://127.0.0.1:8787/webhook/sms")
    p_wh.add_argument("--event", default="sms:received")
    sub.add_parser("webhook-list", help="List gateway webhooks")

    p_plan = sub.add_parser("plan-today", help="Fetch today's Pending tasks and schedule")
    p_plan.add_argument("--dry-run", action="store_true")
    p_plan.add_argument("--day", default=None)
    p_plan.add_argument("--channel", default="SMS", help="SMS, WhatsApp, or Email")

    p_sched = sub.add_parser("run-scheduler", help="Loop: plan + process due (+ Gmail poll)")
    p_sched.add_argument("--once", action="store_true")
    p_sched.add_argument("--dry-run", action="store_true")
    p_sched.add_argument("--day", default=None)
    p_sched.add_argument(
        "--channel",
        default="all",
        help="SMS, WhatsApp, Email, or all",
    )

    p_once = sub.add_parser("run-once", help="Build plan if needed and process due once")
    p_once.add_argument("--dry-run", action="store_true")
    p_once.add_argument("--day", default=None)
    p_once.add_argument("--channel", default="all")
    p_once.add_argument(
        "--force",
        action="store_true",
        help="Bypass uncertain-send cooldown / 【发送结果未确认】 retry guard",
    )

    sub.add_parser("gmail-auth", help="One-time OAuth for Gmail mailbox (browser)")
    sub.add_parser("gmail-poll", help="Poll Gmail history once and ingest replies")

    args = parser.parse_args()
    client = SmsGatewayClient()

    if args.cmd == "ping":
        print(json.dumps(client.ping(), ensure_ascii=False, indent=2))
        return

    if args.cmd == "send":
        resp = client.send_sms([args.to], args.text, sim_number=settings.sms_sim_number)
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        return

    if args.cmd == "serve":
        uvicorn.run(app, host=args.host, port=args.port)
        return

    if args.cmd == "webhook-register":
        print(json.dumps(register_webhook(args.url, args.event), ensure_ascii=False, indent=2))
        return

    if args.cmd == "webhook-list":
        print(json.dumps(list_webhooks(), ensure_ascii=False, indent=2))
        return

    if args.cmd == "plan-today":
        day = date.fromisoformat(args.day) if args.day else ny_today()
        ch = normalize_channel(args.channel)
        plan = build_daily_plan(day=day, channel=ch)
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return

    if args.cmd == "gmail-auth":
        from .gmail_client import run_oauth_desktop_flow

        if not settings.gmail_client_id or not settings.gmail_client_secret:
            raise SystemExit("请先在 .env 配置 GMAIL_CLIENT_ID 与 GMAIL_CLIENT_SECRET")
        tokens = run_oauth_desktop_flow(
            client_id=settings.gmail_client_id,
            client_secret=settings.gmail_client_secret,
        )
        print("\n把下面写入 .env：")
        print(f"GMAIL_REFRESH_TOKEN={tokens['refresh_token']}")
        if settings.gmail_user:
            print(f"GMAIL_USER={settings.gmail_user}")
        else:
            print("GMAIL_USER=mark@fridgechannels.com")
        return

    if args.cmd == "gmail-poll":
        results = poll_gmail_inbound()
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    if args.cmd in {"run-once", "run-scheduler"}:
        day = args.day or ny_today().isoformat()
        channels = (
            ["SMS", "WHATSAPP", "EMAIL"]
            if args.channel.lower() == "all"
            else [normalize_channel(args.channel)]
        )

        force = bool(getattr(args, "force", False))

        last_gmail_poll = 0.0

        def tick() -> None:
            nonlocal last_gmail_poll
            tz = ZoneInfo(settings.timezone)
            now = datetime.now(tz)
            all_results = []
            for ch in channels:
                plan = load_plan(day, ch)
                need_build = plan is None
                if plan is None or (
                    now.hour > settings.daily_plan_hour
                    or (
                        now.hour == settings.daily_plan_hour
                        and now.minute >= settings.daily_plan_minute
                    )
                ):
                    need_build = True
                if need_build:
                    d = date.fromisoformat(day)
                    plan = build_daily_plan(day=d, channel=ch, now=now)
                    log.info("plan ready day=%s channel=%s items=%d", plan.day, ch, len(plan.items))
                results = process_due(
                    day=day, channel=ch, dry_run=args.dry_run, force=force, now=now
                )
                all_results.extend(results)

            # Gmail inbound poll (server-side; independent of email outbound channel filter)
            if settings.gmail_refresh_token and not args.dry_run:
                now_mono = time.time()
                if now_mono - last_gmail_poll >= max(5, settings.gmail_poll_seconds):
                    last_gmail_poll = now_mono
                    try:
                        inbound_results = poll_gmail_inbound()
                        if inbound_results:
                            all_results.append({"gmail_inbound": inbound_results})
                    except Exception:  # noqa: BLE001
                        log.exception("gmail poll failed")

            if all_results:
                print(json.dumps(all_results, ensure_ascii=False, indent=2))

        if args.cmd == "run-once" or args.once:
            tick()
            return

        log.info(
            "scheduler running poll=%ss tz=%s channels=%s",
            settings.scheduler_poll_seconds,
            settings.timezone,
            channels,
        )
        while True:
            try:
                tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            time.sleep(settings.scheduler_poll_seconds)


if __name__ == "__main__":
    main()
