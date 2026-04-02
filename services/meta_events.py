import os
import time
import hashlib
import requests

PIXEL_ID     = "2079654962888572"
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def send_purchase(email: str, valor: float, plano: str, event_id: str = None):
    if not ACCESS_TOKEN:
        print("[META] META_ACCESS_TOKEN não configurado — evento ignorado.")
        return

    payload = {
        "data": [
            {
                "event_name":    "Purchase",
                "event_time":    int(time.time()),
                "event_id":      event_id or f"purchase-{email}-{int(time.time())}",
                "action_source": "website",
                "user_data": {
                    "em": [_hash(email)],
                },
                "custom_data": {
                    "currency": "BRL",
                    "value":    valor,
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
        print(f"[META] Erro ao enviar evento: {e}")
