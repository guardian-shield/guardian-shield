import os
import mercadopago

ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")

sdk = mercadopago.SDK(ACCESS_TOKEN)


# =========================
# CHECKOUT (LINK PAGAMENTO)
# =========================
def criar_pagamento(email, valor, plano):
    try:
        preference_data = {
            "items": [
                {
                    "id": f"guardian-shield-{plano}",
                    "title": f"Guardian Shield — Plano {plano.capitalize()}",
                    "description": f"Licença {plano} de proteção digital Guardian Shield",
                    "category_id": "services",
                    "quantity": 1,
                    "currency_id": "BRL",
                    "unit_price": float(valor)
                }
            ],

            "payer": {
                "email": email,
                "name": "Cliente",
                "surname": "Teste"
            },

            "back_urls": {
                "success": f"https://guardian.grupomayconsantos.com.br/download?email={email}",
                "failure": f"https://guardian.grupomayconsantos.com.br/pagar?email={email}",
                "pending": f"https://guardian.grupomayconsantos.com.br/download?email={email}"
            },

            "auto_return": "approved",

            "payment_methods": {
                "excluded_payment_types": [],
                "excluded_payment_methods": [],
                "installments": 12
            },

            "statement_descriptor": "GUARDIAN",
            "external_reference": f"{email}|{plano}"
        }

        response = sdk.preference().create(preference_data)
        data = response.get("response", {})
        if "init_point" not in data:
            raise Exception(f"MP erro {response.get('status')}: {data}")
        return data["init_point"]

    except Exception as e:
        print("ERRO PAGAMENTO:", e)
        raise


# =========================
# PIX DIRETO
# =========================
def criar_pix(email, valor, plano):
    try:
        payment_data = {
            "transaction_amount": float(valor),
            "description": f"Plano {plano} - Guardian Shield",
            "payment_method_id": "pix",
            "external_reference": f"{email}|{plano}",
            "payer": {
                "email": email
            }
        }

        payment = sdk.payment().create(payment_data)
        return payment["response"]

    except Exception as e:
        print("ERRO PIX:", e)
        return None


# =========================
# CARTÃO (Checkout Bricks)
# =========================
def processar_cartao(email, valor, plano, token, installments, payment_method_id, issuer_id=None):
    payment_data = {
        "transaction_amount": float(valor),
        "token": token,
        "description": f"Plano {plano.capitalize()} - Guardian Shield",
        "installments": int(installments),
        "payment_method_id": payment_method_id,
        "payer": {"email": email},
        "external_reference": f"{email}|{plano}"
    }
    if issuer_id:
        payment_data["issuer_id"] = int(issuer_id)
    payment = sdk.payment().create(payment_data)
    return payment["response"]


# =========================
# BUSCAR PAGAMENTO
# =========================
def buscar_pagamento(payment_id):
    try:
        payment = sdk.payment().get(payment_id)
        return payment.get("response", {})

    except Exception as e:
        print("ERRO:", e)
        return None