"""
notify.py  —  Send email digest when new tenders are found.

Configure via environment variables:
  NOTIFY_EMAIL_TO      recipient address
  NOTIFY_EMAIL_FROM    sender address
  NOTIFY_SMTP_HOST     default: smtp.gmail.com
  NOTIFY_SMTP_PORT     default: 587
  NOTIFY_SMTP_USER     SMTP username (usually same as FROM)
  NOTIFY_SMTP_PASS     SMTP password / app password
"""

import os
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

log = logging.getLogger(__name__)


def send_digest(tenders: list[dict]):
    """Send an HTML email digest of new tenders. No-op if env vars not set."""
    to_addr   = os.getenv("NOTIFY_EMAIL_TO")
    from_addr = os.getenv("NOTIFY_EMAIL_FROM")
    smtp_user = os.getenv("NOTIFY_SMTP_USER", from_addr)
    smtp_pass = os.getenv("NOTIFY_SMTP_PASS")
    smtp_host = os.getenv("NOTIFY_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("NOTIFY_SMTP_PORT", "587"))

    if not all([to_addr, from_addr, smtp_pass]):
        log.info("Email notify skipped — NOTIFY_* env vars not configured.")
        return
    if not tenders:
        return

    subject = f"[Tender Agent] {len(tenders)} new tender(s) — {datetime.utcnow().strftime('%Y-%m-%d')}"

    # ── Build HTML rows ───────────────────────────────────────────────────────
    rows_html = ""
    for t in tenders:
        status_color = {
            "Open": "#27ae60",
            "Closing Soon": "#e67e22",
            "Closed": "#e74c3c",
            "Awarded": "#8e44ad",
        }.get(t.get("status", ""), "#7f8c8d")

        url   = t.get("url") or "#"
        title = t.get("title") or "—"
        link  = f'<a href="{url}" style="color:#2980b9;text-decoration:none;">{title}</a>'

        rows_html += f"""
        <tr style="border-bottom:1px solid #eee;">
          <td style="padding:10px 8px;">{link}<br>
              <small style="color:#666;">{t.get('reference','')}</small></td>
          <td style="padding:10px 8px;">{t.get('issuer','—')}</td>
          <td style="padding:10px 8px;">{t.get('deadline','—')}</td>
          <td style="padding:10px 8px;">{t.get('budget','—')}</td>
          <td style="padding:10px 8px;">
            <span style="background:{status_color};color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;">
              {t.get('status','—')}
            </span>
          </td>
          <td style="padding:10px 8px;font-size:12px;color:#666;">{t.get('source_site','—')}</td>
        </tr>"""

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:900px;margin:auto;color:#333;">
      <h2 style="color:#2c3e50;">Tender Digest — {datetime.utcnow().strftime('%d %B %Y')}</h2>
      <p>{len(tenders)} new tender(s) found across your configured sites.</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#f8f9fa;font-weight:bold;">
            <th style="padding:10px 8px;text-align:left;">Title / Ref</th>
            <th style="padding:10px 8px;text-align:left;">Issuer</th>
            <th style="padding:10px 8px;text-align:left;">Deadline</th>
            <th style="padding:10px 8px;text-align:left;">Budget</th>
            <th style="padding:10px 8px;text-align:left;">Status</th>
            <th style="padding:10px 8px;text-align:left;">Source</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
      <p style="margin-top:24px;font-size:12px;color:#999;">
        Sent by Tender Agent · {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
      </p>
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
        log.info(f"Email digest sent to {to_addr} ({len(tenders)} tenders).")
    except Exception as e:
        log.error(f"Email send failed: {e}")
