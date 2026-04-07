import os
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User
from payment import criar_pix, criar_pagamento, buscar_pagamento, processar_cartao

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# =============================================================
# POST /create-pix  →  cria pagamento PIX direto
# =============================================================
@router.post("/create-pix")
def create_pix(email: str, plano: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if user and plano == "mensal" and user.plan_type == "mensal":
        return {"error": "Plano mensal já utilizado"}

    valor = 99.00 if plano == "mensal" else 399.00 if plano == "anual" else None
    if valor is None:
        return {"error": "Plano inválido"}

    try:
        resultado = criar_pix(email, valor, plano)
    except Exception as e:
        return {"error": f"Falha ao gerar PIX: {e}"}

    if not resultado:
        return {"error": "Falha ao gerar PIX"}

    pix = resultado.get("point_of_interaction", {}).get("transaction_data", {})
    return {
        "payment_id":    resultado.get("id"),
        "qr_code":       pix.get("qr_code"),
        "qr_code_base64": pix.get("qr_code_base64"),
        "valor":         valor,
        "expira_em":     resultado.get("date_of_expiration"),
    }


# =============================================================
# GET /pix-status/{payment_id}  →  verifica status do PIX
# =============================================================
@router.get("/pix-status/{payment_id}")
def pix_status(payment_id: str, db: Session = Depends(get_db)):
    try:
        pagamento = buscar_pagamento(payment_id)
    except Exception as e:
        return {"error": str(e)}

    if not pagamento:
        return {"status": "unknown"}

    status = pagamento.get("status", "unknown")

    # Se aprovado, ativa a licença automaticamente
    if status == "approved":
        from datetime import datetime, timedelta
        reference = pagamento.get("external_reference", "")
        # Suporta separador "|" (novo) e "-" (legado)
        if reference and "|" in reference:
            parts = reference.split("|", 1)
        elif reference and "-" in reference:
            parts = reference.split("-", 1)
        else:
            parts = []
        email = parts[0] if parts else pagamento.get("payer", {}).get("email")
        plano = parts[1] if len(parts) > 1 else "mensal"

        if email:
            user = db.query(User).filter(User.email == email).first()
            sem_licenca = not user.expires_at if user else False
            expirada    = (user and user.expires_at and user.expires_at < datetime.utcnow())
            if user and (sem_licenca or expirada):
                dias = 30 if plano == "mensal" else 365
                user.expires_at = datetime.utcnow() + timedelta(days=dias)
                user.plan_type  = plano
                db.commit()

    return {"status": status}


# =============================================================
# GET /mp-public-key  →  retorna chave pública do Mercado Pago
# =============================================================
@router.get("/mp-public-key")
def mp_public_key():
    return {"public_key": os.getenv("MP_PUBLIC_KEY", "")}


# =============================================================
# POST /process-card  →  processa pagamento com cartão (Bricks)
# =============================================================
@router.post("/process-card")
async def process_card(request: Request):
    data = await request.json()
    email              = data.get("payer", {}).get("email") or data.get("email", "")
    plano              = data.get("plano", "mensal")
    token              = data.get("token")
    installments       = data.get("installments", 1)
    payment_method_id  = data.get("payment_method_id")
    issuer_id          = data.get("issuer_id")

    if not token or not payment_method_id:
        return {"error": "Dados de cartão inválidos"}

    valor = 99.00 if plano == "mensal" else 399.00
    try:
        resultado = processar_cartao(email, valor, plano, token, installments, payment_method_id, issuer_id)
        return {"status": resultado.get("status"), "payment_id": resultado.get("id")}
    except Exception as e:
        return {"error": str(e)}


# =============================================================
# GET /pagar  →  página web de pagamento
# =============================================================
@router.get("/pagar", response_class=HTMLResponse)
def pagina_pagar():
    html_path = os.path.join(os.path.dirname(__file__), "..", "templates", "pagar.html")
    with open(os.path.abspath(html_path), encoding="utf-8") as f:
        return f.read()


# =============================================================
# GET /download  →  página de download após pagamento
# =============================================================
@router.get("/download", response_class=HTMLResponse)
def pagina_download():
    html_path = os.path.join(os.path.dirname(__file__), "..", "templates", "download.html")
    with open(os.path.abspath(html_path), encoding="utf-8") as f:
        return f.read()


# =============================================================
# GET /vendas  →  landing page para anúncios
# =============================================================
@router.get("/vendas", response_class=HTMLResponse)
def pagina_vendas():
    html_path = os.path.join(os.path.dirname(__file__), "..", "templates", "vendas.html")
    with open(os.path.abspath(html_path), encoding="utf-8") as f:
        return f.read()
