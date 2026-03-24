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
                    "title": f"Plano {plano} - Guardian Shield",
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
                "success": "https://google.com",
                "failure": "https://google.com",
                "pending": "https://google.com"
            },

            "auto_return": "approved",

            "payment_methods": {
                "excluded_payment_types": [],
                "excluded_payment_methods": [],
                "installments": 1
            },

            "statement_descriptor": "GUARDIAN",
            "external_reference": f"{email}-{plano}"
        }

        response = sdk.preference().create(preference_data)

        print("\n=== RESPOSTA MP ===")
        print(response)
        print("===================\n")

        return response["response"]["init_point"]

    except Exception as e:
        print("ERRO PAGAMENTO:", e)
        return None


# =========================
# PIX DIRETO
# =========================
def criar_pix(email, valor, plano):
    try:
        payment_data = {
            "transaction_amount": float(valor),
            "description": f"Plano {plano} - Guardian Shield",
            "payment_method_id": "pix",
            "payer": {
                "email": email
            }
        }

        payment = sdk.payment().create(payment_data)

        print("\n=== PIX CRIADO ===")
        print(payment)
        print("==================\n")

        return payment["response"]

    except Exception as e:
        print("ERRO PIX:", e)
        return None


# =========================
# BUSCAR PAGAMENTO
# =========================
def buscar_pagamento(payment_id):
    try:
        payment = sdk.payment().get(payment_id)

        print("\n=== PAGAMENTO DETALHES ===")
        print(payment)
        print("==========================\n")

        return payment.get("response", {})

    except Exception as e:
        print("ERRO:", e)
        return None