from fastapi import APIRouter, Depends, Request, Header
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User
from auth import hash_password, verify_password, create_access_token, verify_token
from payment import criar_pagamento, buscar_pagamento
from datetime import datetime, timedelta
import traceback

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# =========================
# REGISTER
# =========================
@router.post("/register")
def register(email: str, password: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if user:
        return {"error": "Usuário já existe"}

    new_user = User(
        email=email,
        password=hash_password(password),
        plan_type=None,
        expires_at=None,
        hwid_1=None,
        hwid_2=None
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return {"message": "Usuário criado com sucesso"}

# =========================
# LOGIN
# =========================
@router.post("/login")
def login(email: str, password: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()

    if not user:
        return {"error": "Usuário não encontrado"}

    if not verify_password(password, user.password):
        return {"error": "Senha incorreta"}

    token = create_access_token({"sub": user.email})

    return {
        "access_token": token,
        "token_type": "bearer"
    }

# =========================
# PROTECTED COM HWID
# =========================
@router.get("/protected")
def protected_route(
    user=Depends(verify_token),
    db: Session = Depends(get_db),
    x_hwid: str = Header(None)
):
    email = user.get("sub")

    user_db = db.query(User).filter(User.email == email).first()

    if not user_db:
        return {"error": "Usuário não encontrado"}

    # 🔥 BLOQUEIO LICENÇA
    if not user_db.expires_at:
        return {"acesso": False, "motivo": "Sem licença"}

    if user_db.expires_at < datetime.utcnow():
        return {"acesso": False, "motivo": "Licença expirada"}

    # 🔥 HWID CHECK
    if not x_hwid:
        return {"acesso": False, "motivo": "HWID não enviado"}

    # PRIMEIRO ACESSO → REGISTRA
    if not user_db.hwid_1:
        user_db.hwid_1 = x_hwid
        db.commit()

    elif user_db.hwid_1 != x_hwid and not user_db.hwid_2:
        user_db.hwid_2 = x_hwid
        db.commit()

    elif x_hwid != user_db.hwid_1 and x_hwid != user_db.hwid_2:
        return {"acesso": False, "motivo": "Dispositivo não autorizado"}

    return {
        "acesso": True,
        "email": user_db.email,
        "expira_em": user_db.expires_at
    }

# =========================
# CREATE PAYMENT
# =========================
@router.post("/create-payment")
def create_payment(email: str, plano: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()

    if not user:
        return {"error": "Usuário não encontrado"}

    if plano == "mensal" and user.plan_type == "mensal":
        return {"error": "Plano mensal já utilizado"}

    if plano == "mensal":
        valor = 69.90
    elif plano == "anual":
        valor = 397.90
    else:
        return {"error": "Plano inválido"}

    link = criar_pagamento(email, valor, plano)

    return {"payment_url": link}

# =========================
# WEBHOOK
# =========================
@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()

    try:
        if data.get("type") == "payment":
            payment_id = data["data"]["id"]

            pagamento = buscar_pagamento(payment_id)

            if not pagamento:
                return {"status": "erro"}

            status = pagamento.get("status")
            reference = pagamento.get("external_reference")

            if reference:
                email = reference.split("-")[0]
                plano = reference.split("-")[1]
            else:
                email = pagamento.get("payer", {}).get("email")
                plano = "mensal"

            if status == "approved":
                user = db.query(User).filter(User.email == email).first()

                if user:
                    dias = 30 if plano == "mensal" else 365
                    user.expires_at = datetime.utcnow() + timedelta(days=dias)
                    user.plan_type = plano
                    db.commit()

        return {"status": "ok"}

    except Exception as e:
        print("ERRO WEBHOOK:", e)
        traceback.print_exc()
        return {"error": "erro webhook"}
