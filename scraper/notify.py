import os
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

log = logging.getLogger(__name__)


def send_digest(tenders: list):
    to_addr   = os.getenv("NOTIFY_EMAIL_TO")
    from_addr = os.getenv("NOTIFY_EMAIL_FROM")
    smtp_pass = os.getenv("NOTIFY_SMTP_PASS")

    if not all([to_addr, from_addr, smtp_pass]):
        log.info("Email notify skipped — NOTIFY_* env vars not configured.")
        return
    if not tenders:
        return

    smtp_host = os.getenv("NOTIFY_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("NOTIFY_SMTP_PORT", "587"))
    smtp_user = os.getenv("NOTIFY_SMTP_USER", from_addr)

    subject = f"[Tender Agent] {len(tenders)} new tender(s) — {datetime.utcnow().strftime('%Y-%m-%d')}"

    rows_html = ""
    for t in tenders:
        rows_html += f"""
        <tr>
          <td style="padding:8px;">{t.get('title','—')}</td>
          <td style="padding:8px;">{t.get('issuer','—')}</td>
          <td style="padding:8px;">{t.get('deadline','—')}</td>
          <td style="padding:8px;">{t.get('status','—')}</td>
          <td style="padding:8px;">{t.get('source_site','—')}</td>
        </tr>"""

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;">
      <h2>Tender Digest — {datetime.utcnow().strftime('%d %B %Y')}</h2>
      <p>{len(tenders)} new tender(s) found.</p>
      <table border="1" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;">
        <thead>
          <tr style="background:#f0f0f0;">
            <th style="padding:8px;">Title</th>
            <th style="padding:8px;">Issuer</th>
            <th style="padding:8px;">Deadline</th>
            <th style="padding:8px;">Status</th>
            <th style="padding:8px;">Source</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, to_addr, msg.as_string())
        log.info(f"Email digest sent to {to_addr}.")
    except Exception as e:
        log.error(f"Email send failed: {e}")
