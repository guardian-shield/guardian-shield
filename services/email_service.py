import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def get_cfg(db, key, default=None):
    from models import AppConfig
    row = db.query(AppConfig).filter(AppConfig.key == key).first()
    return row.value if row and row.value else default


def send_email(to_email: str, subject: str, html_body: str, db):
    provider = get_cfg(db, "email_provider", "resend")
    if provider == "gmail":
        _send_gmail(to_email, subject, html_body, db)
    else:
        _send_resend(to_email, subject, html_body, db)


def _send_resend(to_email: str, subject: str, html_body: str, db):
    api_key  = get_cfg(db, "resend_api_key")
    from_addr = get_cfg(db, "resend_from", "Guardian Shield <noreply@guardianshield.com.br>")
    if not api_key:
        raise Exception("Resend API key não configurada no painel admin.")
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"from": from_addr, "to": [to_email], "subject": subject, "html": html_body},
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        raise Exception(f"Resend erro {resp.status_code}: {resp.text}")


def _send_gmail(to_email: str, subject: str, html_body: str, db):
    gmail_user = get_cfg(db, "gmail_email")
    gmail_pass = get_cfg(db, "gmail_password")
    print(f"[GMAIL] user={repr(gmail_user)} pass_len={len(gmail_pass) if gmail_pass else 0}")
    if not gmail_user or not gmail_pass:
        raise Exception("Gmail não configurado no painel admin.")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_user
    msg["To"]      = to_email
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, to_email, msg.as_string())


def send_verification_email(to_email: str, nome: str, code: str, db):
    subject = "Guardian Shield — Código de verificação"
    html = f"""
    <div style="font-family:Arial,sans-serif;background:#0a2a5e;padding:40px;text-align:center;">
      <h1 style="color:#d4a017;letter-spacing:2px;">GUARDIAN SHIELD</h1>
      <div style="background:white;border-radius:12px;padding:32px;max-width:400px;margin:0 auto;">
        <p style="color:#333;font-size:16px;">Olá, <b>{nome}</b>!</p>
        <p style="color:#555;">Seu código de verificação de e-mail é:</p>
        <div style="font-size:36px;font-weight:bold;letter-spacing:10px;color:#0a2a5e;
                    background:#f0f4ff;border-radius:8px;padding:16px;margin:20px 0;">
          {code}
        </div>
        <p style="color:#888;font-size:12px;">Válido por 15 minutos. Não compartilhe este código.</p>
      </div>
    </div>
    """
    send_email(to_email, subject, html, db)
