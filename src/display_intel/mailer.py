"""寄信：用 Gmail（或任何 SMTP）寄出 HTML 報告與 Excel 附件。"""

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path

from .config import env

log = logging.getLogger(__name__)


def send_report(subject: str, html: str, attachments: list[Path], recipients: list[str] | None = None) -> None:
    user = env("SMTP_USER", required=True)
    password = env("SMTP_PASSWORD", required=True).replace(" ", "")  # Google 顯示的應用程式密碼含空格
    to = recipients or [r.strip() for r in env("REPORT_RECIPIENTS", required=True).split(",") if r.strip()]
    host = env("SMTP_HOST") or "smtp.gmail.com"
    port = int(env("SMTP_PORT") or 465)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg.set_content("您的郵件程式不支援 HTML，請開啟附件或改用其他郵件程式查看。")
    msg.add_alternative(html, subtype="html")
    for path in attachments:
        msg.add_attachment(path.read_bytes(), maintype="application",
                           subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=path.name)

    last_exc = None
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=60) as s:
                s.login(user, password)
                s.send_message(msg)
            log.info("報告已寄出給 %d 位收件人", len(to))
            return
        except smtplib.SMTPAuthenticationError:
            raise RuntimeError("Gmail 登入失敗：請確認 SMTP_USER 與 SMTP_PASSWORD（要用應用程式密碼，不是登入密碼）")
        except Exception as exc:
            last_exc = exc
            log.warning("寄信失敗（第 %d 次）：%s", attempt + 1, exc)
            time.sleep(30)
    raise RuntimeError(f"寄信失敗：{last_exc}")
