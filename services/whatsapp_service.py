import requests
import re


def get_cfg(db, key, default=None):
    from models import AppConfig
    row = db.query(AppConfig).filter(AppConfig.key == key).first()
    return row.value if row and row.value else default


def _format_number(number: str) -> str:
    """Garante formato 55DDNÚMERO sem caracteres especiais."""
    digits = re.sub(r"\D", "", number)
    if not digits.startswith("55"):
        digits = "55" + digits
    return digits


def send_whatsapp_message(number: str, message: str, db) -> bool:
    url      = get_cfg(db, "evolution_api_url")
    api_key  = get_cfg(db, "evolution_api_key")
    instance = get_cfg(db, "evolution_instance")

    if not url or not api_key or not instance:
        raise Exception("Evolution API não configurada no painel admin.")

    endpoint = f"{url.rstrip('/')}/message/sendText/{instance}"
    payload  = {"number": _format_number(number), "text": message}

    resp = requests.post(
        endpoint,
        headers={"apikey": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )
    return resp.status_code in (200, 201)


def send_verification_whatsapp(number: str, nome: str, code: str, db) -> bool:
    message = (
        f"*Guardian Shield*\n\n"
        f"Olá, {nome}!\n\n"
        f"Seu código de verificação do WhatsApp é:\n\n"
        f"*{code}*\n\n"
        f"Válido por 15 minutos. Não compartilhe este código."
    )
    return send_whatsapp_message(number, message, db)
