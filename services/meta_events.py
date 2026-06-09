import os
import time
import hashlib
import requests

PIXEL_ID     = "2079654962888572"
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")


def _hash(value: str):
    if not value:
        return None
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def send_purchase(
    email: str,
    valor: float,
    plano: str,
    event_id: str = None,
    phone: str = None,
    external_id = None,
    fbc: str = None,
    fbp: str = None,
    client_ip: str = None,
    client_ua: str = None,
):
    if not ACCESS_TOKEN:
        print("[META] META_ACCESS_TOKEN não configurado — evento ignorado.")
        return

    user_data = {}

    em = _hash(email)
    if em:
        user_data["em"] = [em]

    if phone:
        ph_clean = "".join(filter(str.isdigit, phone))
        if ph_clean and not ph_clean.startswith("55"):
            ph_clean = "55" + ph_clean
        ph_hash = _hash(ph_clean)
        if ph_hash:
            user_data["ph"] = [ph_hash]

    ext = str(external_id) if external_id else email
    ext_hash = _hash(ext)
    if ext_hash:
        user_data["external_id"] = [ext_hash]

    if fbc:
        user_data["fbc"] = fbc
    if fbp:
        user_data["fbp"] = fbp
    if client_ip:
        user_data["client_ip_address"] = client_ip
    if client_ua:
        user_data["client_user_agent"] = client_ua

    payload = {
        "data": [
            {
                "event_name":    "Purchase",
                "event_time":    int(time.time()),
                "event_id":      event_id or f"purchase-{_hash(email)}-{int(time.time())}",
                "action_source": "website",
                "user_data":     user_data,
                "custom_data": {
                    "currency":     "BRL",
                    "value":        valor,
                    "content_ids":  [plano],
                    "content_type": "product",
                    "content_name": f"Guardian Shield {plano.capitalize()}",
                },
            }
        ],
        "access_token": ACCESS_TOKEN,
    }

    try:
        resp = requests.post(
            f"https://graph.facebook.com/v19.0/{PIXEL_ID}/events",
            json=payload,
            timeout=10,
        )
        print(f"[META] Purchase enviado ({email}): {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[META] Erro ao enviar Purchase: {e}")
