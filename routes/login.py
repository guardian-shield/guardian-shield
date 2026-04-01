import os
import hmac
import hashlib
import random
import traceback
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, Request, HTTPException
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User
from auth import hash_password, verify_password, create_access_token, verify_token
from payment import criar_pagamento, buscar_pagamento
from services.email_service import send_verification_email
from services.whatsapp_service import send_verification_whatsapp

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _gerar_codigo() -> str:
    return str(random.randint(100000, 999999))


# =============================================================
# REGISTER
# =============================================================
@router.post("/register")
@limiter.limit("5/minute")
def register(
    request: Request,
    email: str,
    password: str,
    nome: str = "",
    whatsapp: str = "",
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email).first()
    code = _gerar_codigo()
    expires = datetime.utcnow() + timedelta(minutes=15)

    if user:
        # Usuário pré-liberado (pagou antes de cadastrar) — completa o registro
        if user.pre_liberado:
            user.nome               = nome or user.nome
            user.password           = hash_password(password)
            user.whatsapp           = whatsapp or user.whatsapp
            user.email_code         = code
            user.email_code_expires = expires
            user.email_verified     = False
            user.pre_liberado       = False
            db.commit()
            _tentar_enviar_email(email, nome, code, db)
            return {"message": "Cadastro completado! Verifique seu e-mail."}

        return {"error": "E-mail já cadastrado"}

    new_user = User(
        nome               = nome,
        email              = email,
        password           = hash_password(password),
        whatsapp           = whatsapp,
        email_verified     = False,
        whatsapp_verified  = False,
        email_code         = code,
        email_code_expires = expires,
    )
    db.add(new_user)
    db.commit()
    _tentar_enviar_email(email, nome, code, db)
    return {"message": "Cadastro realizado! Verifique seu e-mail para ativar a conta."}


def _tentar_enviar_email(email, nome, code, db):
    try:
        send_verification_email(email, nome or email, code, db)
    except Exception as e:
        print(f"[EMAIL] Falha ao enviar verificação para {email}: {e}")


# =============================================================
# VERIFY EMAIL
# =============================================================
@router.post("/verify-email")
def verify_email(email: str, code: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "Usuário não encontrado"}
    if user.email_verified:
        return {"message": "E-mail já verificado"}
    if not user.email_code or user.email_code != code:
        return {"error": "Código inválido"}
    if user.email_code_expires and datetime.utcnow() > user.email_code_expires:
        return {"error": "Código expirado. Solicite um novo."}

    user.email_verified = True
    user.email_code     = None
    db.commit()
    return {"message": "E-mail verificado com sucesso!"}


# =============================================================
# REENVIAR CÓDIGO DE EMAIL
# =============================================================
@router.post("/resend-code")
@limiter.limit("3/minute")
def resend_code(request: Request, email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "Usuário não encontrado"}
    if user.email_verified:
        return {"message": "E-mail já verificado"}

    code = _gerar_codigo()
    user.email_code         = code
    user.email_code_expires = datetime.utcnow() + timedelta(minutes=15)
    db.commit()
    _tentar_enviar_email(email, user.nome, code, db)
    return {"message": "Novo código enviado!"}


# =============================================================
# LOGIN
# =============================================================
@router.post("/login")
@limiter.limit("10/minute")
def login(request: Request, email: str, password: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "Usuário não encontrado"}
    if user.pre_liberado:
        return {"error": "cadastro_pendente", "plan_type": user.plan_type}
    if not user.password or not verify_password(password, user.password):
        return {"error": "Senha incorreta"}
    if not user.email_verified:
        return {"error": "E-mail não verificado. Cheque sua caixa de entrada."}

    token = create_access_token({"sub": user.email})
    return {"access_token": token, "token_type": "bearer"}


# =============================================================
# PROTECTED — valida licença + HWID + WhatsApp
# =============================================================
@router.get("/protected")
def protected_route(
    user=Depends(verify_token),
    db: Session = Depends(get_db),
    x_hwid: str = Header(None),
):
    email   = user.get("sub")
    user_db = db.query(User).filter(User.email == email).first()

    if not user_db:
        return {"acesso": False, "motivo": "Usuário não encontrado"}
    if not user_db.expires_at:
        return {"acesso": False, "motivo": "Sem licença", "plan_type": user_db.plan_type}
    if user_db.expires_at < datetime.utcnow():
        return {"acesso": False, "motivo": "Licença expirada", "plan_type": user_db.plan_type}
    if not x_hwid:
        return {"acesso": False, "motivo": "HWID não enviado"}

    # Registro de HWID
    if not user_db.hwid_1:
        user_db.hwid_1 = x_hwid
        db.commit()
    elif user_db.hwid_1 != x_hwid and not user_db.hwid_2:
        user_db.hwid_2 = x_hwid
        db.commit()
    elif x_hwid != user_db.hwid_1 and x_hwid != user_db.hwid_2:
        return {"acesso": False, "motivo": "Dispositivo não autorizado"}

    # Verificação do WhatsApp (exigida no primeiro acesso após licença ativa)
    if not user_db.whatsapp_verified:
        # envia código se ainda não enviou ou se expirou
        if (
            not user_db.whatsapp_code
            or not user_db.whatsapp_code_expires
            or datetime.utcnow() > user_db.whatsapp_code_expires
        ):
            if user_db.whatsapp:
                code = _gerar_codigo()
                user_db.whatsapp_code         = code
                user_db.whatsapp_code_expires = datetime.utcnow() + timedelta(minutes=15)
                db.commit()
                try:
                    send_verification_whatsapp(user_db.whatsapp, user_db.nome or email, code, db)
                except Exception as e:
                    print(f"[WA] Falha ao enviar código WA para {user_db.whatsapp}: {e}")

        return {
            "acesso": False,
            "motivo": "whatsapp_nao_verificado",
            "whatsapp": user_db.whatsapp,
        }

    return {
        "acesso":     True,
        "email":      user_db.email,
        "nome":       user_db.nome,
        "expira_em":  user_db.expires_at,
        "plan_type":  user_db.plan_type,
    }


# =============================================================
# VERIFY WHATSAPP (chamado pelo app Electron)
# =============================================================
@router.post("/verify-whatsapp")
def verify_whatsapp(
    code: str,
    user=Depends(verify_token),
    db: Session = Depends(get_db),
):
    email   = user.get("sub")
    user_db = db.query(User).filter(User.email == email).first()
    if not user_db:
        return {"error": "Usuário não encontrado"}
    if user_db.whatsapp_verified:
        return {"message": "WhatsApp já verificado"}
    if not user_db.whatsapp_code or user_db.whatsapp_code != code:
        return {"error": "Código inválido"}
    if user_db.whatsapp_code_expires and datetime.utcnow() > user_db.whatsapp_code_expires:
        return {"error": "Código expirado"}

    user_db.whatsapp_verified = True
    user_db.whatsapp_code     = None
    db.commit()
    return {"message": "WhatsApp verificado com sucesso!"}


# =============================================================
# USER PLAN — retorna plan_type do usuário (para ocultar mensal já usado)
# =============================================================
@router.get("/user-plan")
def user_plan(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"plan_type": None}
    return {"plan_type": user.plan_type}


# =============================================================
# CREATE PAYMENT
# =============================================================
@router.post("/create-payment")
def create_payment(email: str, plano: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if user and plano == "mensal" and user.plan_type == "mensal":
        return {"error": "Plano mensal já utilizado"}
    valor = 69.90 if plano == "mensal" else 397.90 if plano == "anual" else None
    if valor is None:
        return {"error": "Plano inválido"}
    try:
        link = criar_pagamento(email, valor, plano)
    except Exception as e:
        return {"error": f"Falha ao gerar pagamento: {e}"}
    return {"payment_url": link}


# =============================================================
# WEBHOOK MERCADO PAGO
# =============================================================
def _verificar_assinatura_mp(request_body: bytes, x_signature: str | None, x_request_id: str | None, query_data_id: str | None) -> bool:
    """Valida a assinatura HMAC-SHA256 enviada pelo Mercado Pago."""
    secret = os.getenv("MP_WEBHOOK_SECRET")
    if not secret:
        # Se não configurou o secret, aceita (compatibilidade retroativa)
        return True
    if not x_signature:
        return False
    # Monta o manifest: ts + request_id + data.id
    parts = {}
    for part in x_signature.split(","):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            parts[k] = v
    ts  = parts.get("ts", "")
    v1  = parts.get("v1", "")
    manifest = f"id:{query_data_id};request-id:{x_request_id};ts:{ts};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


@router.post("/webhook")
async def webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_signature: str = Header(None),
    x_request_id: str = Header(None),
):
    body = await request.body()
    try:
        data = __import__("json").loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Body inválido")

    query_data_id = request.query_params.get("data.id") or (
        str(data.get("data", {}).get("id", "")) if isinstance(data.get("data"), dict) else ""
    )

    if not _verificar_assinatura_mp(body, x_signature, x_request_id, query_data_id):
        raise HTTPException(status_code=401, detail="Assinatura inválida")

    try:
        if data.get("type") == "payment":
            payment_id = data["data"]["id"]
            pagamento  = buscar_pagamento(payment_id)
            if not pagamento:
                return {"status": "erro"}

            status    = pagamento.get("status")
            reference = pagamento.get("external_reference")

            if reference:
                parts = reference.split("-")
                email = parts[0]
                plano = parts[1] if len(parts) > 1 else "mensal"
            else:
                email = pagamento.get("payer", {}).get("email")
                plano = "mensal"

            if status == "approved" and email:
                user = db.query(User).filter(User.email == email).first()
                dias = 30 if plano == "mensal" else 365

                if user:
                    user.expires_at = datetime.utcnow() + timedelta(days=dias)
                    user.plan_type  = plano
                else:
                    # Pagou antes de se cadastrar — pré-libera o email
                    user = User(
                        email        = email,
                        pre_liberado = True,
                        expires_at   = datetime.utcnow() + timedelta(days=dias),
                        plan_type    = plano,
                    )
                    db.add(user)
                db.commit()

        return {"status": "ok"}
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}
