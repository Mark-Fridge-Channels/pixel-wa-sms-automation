from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, datetime

import uvicorn

from .channels import normalize_channel
from .config import settings
from .gateway import SmsGatewayClient
from .inbound import ingest_gmail_messages, poll_gmail_inbound
from .runtime_settings import get_scan_interval_seconds
from .scan_runtime import drain_until_idle, scan_and_enqueue, scheduler_tick
from .scheduler import build_daily_plan, ny_today
from .server import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cli")


def register_webhook(url: str, event: str = "sms:received") -> dict:
    client = SmsGatewayClient()
    return client.register_webhook(url, event=event)


def list_webhooks() -> list:
    client = SmsGatewayClient()
    return client.list_webhooks()


def _channel_list(raw: str) -> list[str]:
    if raw.lower() == "all":
        return ["SMS", "WHATSAPP", "EMAIL"]
    return [normalize_channel(raw)]


def _find_sent_gmail_id(gmail, email: str, subject: str | None) -> str | None:
    from .email_util import normalize_subject
    from .external_send import _gmail_meta

    want = normalize_subject(subject) if subject else ""
    for mid in gmail.list_message_ids(f"in:sent to:{email} newer_than:60d", max_results=10):
        if not want:
            return mid
        if normalize_subject(_gmail_meta(gmail, mid).get("subject") or "") == want:
            return mid
    return None


def _register_email_sent(args: argparse.Namespace) -> dict:
    from .external_send import register_external_email_sent
    from .gmail_client import GmailClient
    from .notion_client import NotionClient

    import re

    from .notion_props import props, rich_text_plain

    notion = NotionClient()
    task_id = args.task_id.strip()
    status = notion.get_task_status(task_id)
    resolved = notion.resolve_task_for_send(task_id)
    if resolved.channel != "EMAIL":
        raise SystemExit(f"任务不是 Email 渠道: {resolved.channel}")
    # superExhibition links the Task to an arbitrary Follow-up Contact; the real
    # recipient is the "To: x" line it writes into Task Notes.
    notes = rich_text_plain(props(notion.get_page(task_id)).get("Notes")) or ""
    notes_to = re.search(r"^To:\s*(\S+@\S+)", notes, flags=re.M)
    email = args.email.strip() or (notes_to.group(1) if notes_to else None) or resolved.email
    thread_id = args.thread_id.strip() or resolved.thread_id
    subject = args.subject.strip() or resolved.subject
    ext = resolved.extended_parameters or {}
    gmail_thread_id = args.gmail_thread_id.strip() or ext.get("gmailThreadId") or ext.get("gmail_thread_id")
    gmail_message_id = args.gmail_message_id.strip() or ext.get("gmailMessageId") or ext.get("gmail_message_id")
    if not email or not thread_id:
        raise SystemExit(f"无法从任务解析收件人或 Thread ID: email={email!r} thread={thread_id!r}")

    gmail = GmailClient()
    if not gmail_message_id and not gmail_thread_id:
        gmail_message_id = _find_sent_gmail_id(gmail, email, subject)

    plan = {
        "task_id": task_id,
        "task_status": status,
        "email": email,
        "thread_id": thread_id,
        "conversation_id": resolved.conversation_id,
        "subject": subject,
        "gmail_thread_id": gmail_thread_id,
        "gmail_message_id": gmail_message_id,
        "warning": None if status == "Completed" else "任务不是 Completed，Portal 会拒收回信（409）",
    }
    if args.dry_run:
        return {"dry_run": True, **plan}
    return register_external_email_sent(
        email=email,
        task_id=task_id,
        thread_id=thread_id,
        gmail_thread_id=gmail_thread_id,
        gmail_message_id=gmail_message_id,
        subject=subject,
        sent_at=args.sent_at.strip() or None,
        contact_page_id=resolved.contact_id,
        conversation_page_id=resolved.conversation_id,
        source="cli",
        gmail=gmail,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="channel_orchestrator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="Ping SMS gateway")

    p_send = sub.add_parser("send", help="Send one SMS via gateway")
    p_send.add_argument("--to", required=True)
    p_send.add_argument("--text", required=True)

    p_serve = sub.add_parser("serve", help="Run webhook + WA job API + monitor server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8787)

    p_wh = sub.add_parser("webhook-register", help="Register sms:received webhook on gateway")
    p_wh.add_argument("--url", default="http://127.0.0.1:8787/webhook/sms")
    p_wh.add_argument("--event", default="sms:received")
    sub.add_parser("webhook-list", help="List gateway webhooks")

    p_plan = sub.add_parser(
        "plan-today",
        help="(legacy) Build daily_plan JSON from today's Pending tasks",
    )
    p_plan.add_argument("--dry-run", action="store_true")
    p_plan.add_argument("--day", default=None)
    p_plan.add_argument("--channel", default="SMS", help="SMS, WhatsApp, or Email")

    p_sched = sub.add_parser(
        "run-scheduler",
        help="Loop: scan Notion due Pending → queue → SMS/WA serial + Email parallel (+ Gmail poll)",
    )
    p_sched.add_argument("--once", action="store_true")
    p_sched.add_argument("--dry-run", action="store_true")
    p_sched.add_argument(
        "--channel",
        default="all",
        help="SMS, WhatsApp, Email, or all",
    )
    p_sched.add_argument(
        "--force",
        action="store_true",
        help="Bypass uncertain-send cooldown / 【发送结果未确认】 retry guard",
    )

    p_once = sub.add_parser("run-once", help="Scan due tasks now, drain queue once")
    p_once.add_argument("--dry-run", action="store_true")
    p_once.add_argument("--channel", default="all")
    p_once.add_argument(
        "--scan-now",
        action="store_true",
        default=True,
        help="Immediately scan Notion (default on)",
    )
    p_once.add_argument(
        "--no-drain",
        action="store_true",
        help="Only enqueue; do not execute/drain",
    )
    p_once.add_argument(
        "--force",
        action="store_true",
        help="Bypass uncertain-send cooldown / 【发送结果未确认】 retry guard",
    )

    sub.add_parser("gmail-auth", help="One-time OAuth for Gmail mailbox (browser)")
    sub.add_parser("gmail-poll", help="Poll Gmail history once and ingest replies")
    p_ingest = sub.add_parser(
        "gmail-ingest",
        help="Ingest one already-received Gmail message (replay a skipped auto-reply)",
    )
    p_ingest.add_argument("--message-id", default="", help="Gmail API message id")
    p_ingest.add_argument(
        "--query",
        default="",
        help="Gmail search query, used when --message-id is empty",
    )
    p_ingest.add_argument(
        "--force",
        action="store_true",
        help="Replay even if this Gmail id was already marked seen",
    )
    p_ext = sub.add_parser(
        "register-email-sent",
        help="Register an email sent outside the orchestrator (fills the Email outbound cache from a Notion task)",
    )
    p_ext.add_argument("--task-id", required=True, help="Notion task page id (Completed Email task)")
    p_ext.add_argument("--email", default="", help="Recipient; default from the task's Key Person")
    p_ext.add_argument("--thread-id", default="", help="Portal Thread ID; default from the task's Conversation")
    p_ext.add_argument("--gmail-message-id", default="", help="Gmail API id of the sent message")
    p_ext.add_argument("--gmail-thread-id", default="")
    p_ext.add_argument("--subject", default="")
    p_ext.add_argument("--sent-at", default="", help="ISO time; default from Gmail")
    p_ext.add_argument("--dry-run", action="store_true", help="Resolve and print only; do not write the cache")
    sub.add_parser(
        "sync-client-domains",
        help="Sync Follow-up Client → Client.Domain cache (full table)",
    )

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
            print("GMAIL_USER=ella@fridgechannels.com")
        return

    if args.cmd == "gmail-poll":
        results = poll_gmail_inbound()
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    if args.cmd == "gmail-ingest":
        from .gmail_client import GmailClient

        ids = [args.message_id.strip()] if args.message_id.strip() else []
        if not ids:
            query = (args.query or "").strip()
            if not query:
                raise SystemExit("请提供 --message-id 或 --query")
            ids = GmailClient().list_message_ids(query, max_results=5)
            if not ids:
                raise SystemExit(f"Gmail 没有搜到邮件: {query}")
            if len(ids) > 1:
                raise SystemExit(
                    "搜到多封邮件，请改用 --message-id 指定一封: " + ", ".join(ids)
                )
        results = ingest_gmail_messages(ids, force=bool(args.force))
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    if args.cmd == "register-email-sent":
        print(json.dumps(_register_email_sent(args), ensure_ascii=False, indent=2, default=str))
        return

    if args.cmd == "sync-client-domains":
        from .client_domain_cache import get_domain_cache

        result = get_domain_cache().sync()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.cmd == "run-once":
        channels = _channel_list(args.channel)
        force = bool(args.force)
        enqueued = []
        if args.scan_now:
            enqueued = scan_and_enqueue(channels=channels)
        results = []
        if not args.no_drain:
            results = drain_until_idle(dry_run=args.dry_run, force=force)
        payload = {"enqueued": enqueued, "results": results}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.cmd == "run-scheduler":
        channels = _channel_list(args.channel)
        force = bool(args.force)
        last_gmail_poll = 0.0
        last_scan = 0.0
        last_domain_sync = 0.0

        def tick(*, force_scan: bool = False) -> None:
            nonlocal last_gmail_poll, last_scan, last_domain_sync
            now_mono = time.time()
            scan_interval = get_scan_interval_seconds()
            do_scan = force_scan or args.once or (now_mono - last_scan >= scan_interval)
            out = scheduler_tick(
                channels=channels,
                dry_run=args.dry_run,
                force=force,
                scan=do_scan,
                drain=True,
            )
            if do_scan:
                last_scan = now_mono

            sync_every = max(60, int(settings.client_domain_sync_seconds or 0))
            if (
                settings.notion_token
                and settings.notion_followup_client_ds
                and not args.dry_run
                and (force_scan or now_mono - last_domain_sync >= sync_every)
            ):
                last_domain_sync = now_mono
                try:
                    from .client_domain_cache import get_domain_cache

                    sync_result = get_domain_cache().sync()
                    out["client_domain_sync"] = sync_result
                except Exception:  # noqa: BLE001
                    log.exception("client domain sync failed")

            if settings.gmail_refresh_token and not args.dry_run:
                if now_mono - last_gmail_poll >= max(5, settings.gmail_poll_seconds):
                    last_gmail_poll = now_mono
                    try:
                        inbound_results = poll_gmail_inbound()
                        if inbound_results:
                            out["gmail_inbound"] = inbound_results
                    except Exception:  # noqa: BLE001
                        log.exception("gmail poll failed")

            if (
                out.get("enqueued")
                or out.get("results")
                or out.get("gmail_inbound")
                or out.get("client_domain_sync")
            ):
                print(json.dumps(out, ensure_ascii=False, indent=2))

            if not args.dry_run:
                try:
                    from .device_alert import maybe_alert_device_health

                    alert = maybe_alert_device_health()
                    if alert.get("sent") or alert.get("ok") is False:
                        out["device_alert"] = alert
                        print(json.dumps({"device_alert": alert}, ensure_ascii=False, indent=2))
                except Exception:  # noqa: BLE001
                    log.exception("device alert check failed")

        if args.once:
            tick(force_scan=True)
            # Drain remaining phone jobs with gaps for --once.
            if not args.dry_run:
                more = drain_until_idle(dry_run=args.dry_run, force=force, max_seconds=min(600, get_scan_interval_seconds() * 2))
                if more:
                    print(json.dumps({"drained": more}, ensure_ascii=False, indent=2))
            return

        log.info(
            "scheduler running scan=%ss send_gap from runtime; channels=%s tz=%s",
            get_scan_interval_seconds(),
            channels,
            settings.timezone,
        )
        # Immediate first scan so deploy doesn't wait a full interval.
        tick(force_scan=True)
        while True:
            try:
                tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            time.sleep(1)


if __name__ == "__main__":
    main()
