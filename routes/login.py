import os
import hmac
import hashlib
import random
import traceback
import logging

logger = logging.getLogger("guardian")
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, Request, HTTPException
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User
from auth import hash_password, verify_password, create_access_token, verify_token
from payment import criar_pagamento, buscar_pagamento
from services.whatsapp_service import send_verification_whatsapp
from services.meta_events import send_purchase as meta_send_purchase

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

    if user:
        # Usuário pré-liberado (pagou antes de cadastrar) — completa o registro
        if user.pre_liberado:
            user.nome           = nome or user.nome
            user.password       = hash_password(password)
            user.whatsapp       = whatsapp or user.whatsapp
            user.email_verified = True
            user.pre_liberado   = False
            db.commit()

            # Envia código WA para verificação (igual ao cadastro normal)
            wa = user.whatsapp
            if wa:
                code = _gerar_codigo()
                user.whatsapp_code         = code
                user.whatsapp_code_expires = datetime.utcnow() + timedelta(minutes=15)
                db.commit()
                try:
                    logger.warning(f"[WA] Enviando código pre_liberado {code} para {wa}")
                    send_verification_whatsapp(wa, user.nome or email, code, db)
                except Exception as e:
                    logger.error(f"[WA] Falha ao enviar código pre_liberado: {e}")

            return {"message": "Cadastro completado!"}

        return {"error": "E-mail já cadastrado"}

    new_user = User(
        nome              = nome,
        email             = email,
        password          = hash_password(password),
        whatsapp          = whatsapp,
        email_verified    = True,
        whatsapp_verified = False,
    )
    db.add(new_user)
    db.commit()

    # Envia código WhatsApp imediatamente após cadastro
    if whatsapp:
        code = _gerar_codigo()
        new_user.whatsapp_code         = code
        new_user.whatsapp_code_expires = datetime.utcnow() + timedelta(minutes=15)
        db.commit()
        try:
            logger.warning(f"[WA] Tentando enviar código {code} para {whatsapp}")
            send_verification_whatsapp(whatsapp, nome or email, code, db)
            logger.warning(f"[WA] Código enviado com sucesso para {whatsapp}")
        except Exception as e:
            logger.error(f"[WA] Falha ao enviar código no cadastro: {e}")

    return {"message": "Cadastro realizado!"}


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
        return {"plan_type": None, "trial_usado": False}
    return {"plan_type": user.plan_type, "trial_usado": bool(user.trial_usado)}


# =============================================================
# CREATE PAYMENT
# =============================================================
@router.post("/create-payment")
def create_payment(email: str, plano: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if user and plano == "mensal" and user.plan_type == "mensal":
        return {"error": "Plano mensal já utilizado"}
    valor = 99.00 if plano == "mensal" else 399.00 if plano == "anual" else None
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

            if reference and "|" in reference:
                parts = reference.split("|", 1)
                email = parts[0]
                plano = parts[1] if len(parts) > 1 else "mensal"
            elif reference and "-" in reference:
                parts = reference.split("-", 1)
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
                    # Se não tem senha ainda, marca pre_liberado para liberar cadastro no app
                    if not user.password:
                        user.pre_liberado = True
                else:
                    user = User(
                        email        = email,
                        pre_liberado = True,
                        expires_at   = datetime.utcnow() + timedelta(days=dias),
                        plan_type    = plano,
                    )
                    db.add(user)
                db.commit()

                # Envia evento de compra para o Meta Conversions API
                valor = 99.00 if plano == "mensal" else 399.00
                meta_send_purchase(email, valor, plano, event_id=str(payment_id))

                # Mensagem WhatsApp de confirmação de pagamento + link de download
                if user and user.whatsapp:
                    try:
                        plano_nome = "Mensal" if plano == "mensal" else "Anual"
                        msg_confirmacao = (
                            f"✅ *Pagamento confirmado!*\n\n"
                            f"Olá, {user.nome or email}!\n\n"
                            f"Seu plano *Guardian Shield {plano_nome}* foi ativado com sucesso.\n\n"
                            f"📥 *Baixe o aplicativo pelo link abaixo:*\n"
                            f"https://drive.google.com/uc?export=download&id=1IF5gPconoMyfDU8HKLPIaMGlHu5UaIL4\n\n"
                            f"Após instalar, abra o app, clique em *Cadastro*, use o e-mail acima e crie sua senha. Em seguida verifique seu WhatsApp para ativar o acesso.\n\n"
                            f"Qualquer dúvida, é só chamar! 🛡️"
                        )
                        from services.whatsapp_service import send_whatsapp_message
                        send_whatsapp_message(user.whatsapp, msg_confirmacao, db)
                    except Exception as e:
                        logger.error(f"[WA] Falha ao enviar confirmação de pagamento: {e}")

                # Notificação de venda para o dono
                try:
                    from services.whatsapp_service import send_whatsapp_message
                    plano_nome = "Mensal (R$99)" if plano == "mensal" else "Anual (R$499)"
                    msg_dono = (
                        f"🔔 *Nova venda Guardian Shield!*\n\n"
                        f"💰 Plano: *{plano_nome}*\n"
                        f"📧 Cliente: {email}\n"
                        f"📱 WhatsApp: {user.whatsapp if user and user.whatsapp else 'não informado'}\n\n"
                        f"✅ Licença ativada automaticamente."
                    )
                    send_whatsapp_message("45998452596", msg_dono, db)
                except Exception as e:
                    logger.error(f"[WA] Falha ao notificar dono sobre venda: {e}")

            elif status in ("refunded", "charged_back") and email:
                # Estorno — cancela a licença imediatamente
                user = db.query(User).filter(User.email == email).first()
                if user:
                    user.expires_at = datetime.utcnow()
                    db.commit()
                    logger.warning(f"[ESTORNO] Licença cancelada para {email} — status: {status}")

                    # Avisa o usuário via WhatsApp
                    if user.whatsapp:
                        try:
                            msg_estorno = (
                                f"⚠️ *Acesso cancelado*\n\n"
                                f"Olá, {user.nome or email}.\n\n"
                                f"Identificamos um estorno no seu pagamento e seu acesso ao *Guardian Shield* foi cancelado.\n\n"
                                f"Se acha que isso foi um engano, entre em contato conosco."
                            )
                            from services.whatsapp_service import send_whatsapp_message
                            send_whatsapp_message(user.whatsapp, msg_estorno, db)
                        except Exception as e:
                            logger.error(f"[WA] Falha ao enviar aviso de estorno: {e}")

        return {"status": "ok"}
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}
