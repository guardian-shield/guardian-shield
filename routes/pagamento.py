import os
import asyncio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User
from payment import criar_pix, criar_pagamento, buscar_pagamento, processar_cartao

router = APIRouter()


def _registrar_lead_crm(phone: str, email: str, plano: str, db, nome: str = ""):
    """Cria ou atualiza conversa no CRM assim que lead informa dados — garante follow-up mesmo se PIX falhar."""
    from models import CrmConversation, CrmMessage
    from datetime import datetime

    phone_clean = phone.replace("+", "").replace(" ", "").replace("-", "")

    conv = db.query(CrmConversation).filter(CrmConversation.phone == phone_clean).first()
    if not conv:
        conv = CrmConversation(
            phone=phone_clean,
            contact_name=nome or None,
            contact_email=email,
            stage="initiated",
            ai_active=True,
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)
        db.add(CrmMessage(
            conversation_id=conv.id,
            direction="out",
            content=f"[Sistema] Lead entrou na página de pagamento — plano {plano}.",
            sent_by="system",
        ))
        db.commit()
    else:
        if conv.stage == "lead":
            conv.stage = "initiated"
        if not conv.contact_email:
            conv.contact_email = email
        if nome and not conv.contact_name:
            conv.contact_name = nome
        conv.updated_at = datetime.utcnow()
        db.commit()


async def _abandono_pix(payment_id: str, phone: str):
    """5 minutos após PIX gerado: se não pagou, Maia entra em contato via CRM."""
    await asyncio.sleep(300)  # 5 minutos
    db = SessionLocal()
    try:
        pagamento = buscar_pagamento(payment_id)
        if pagamento and pagamento.get("status") == "approved":
            return  # já pagou

        from models import CrmConversation, CrmMessage
        from services.whatsapp_service import send_whatsapp_message
        from datetime import datetime

        phone_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
        conv = db.query(CrmConversation).filter(CrmConversation.phone == phone_clean).first()
        if not conv:
            return

        msg = (
            "Oi! Vi que você gerou o PIX do Guardian Shield mas ainda não finalizou. "
            "Ficou alguma dúvida? Posso te ajudar agora mesmo 😊\n\n"
            "Se quiser, gera um novo PIX aqui: https://guardian.grupomayconsantos.com.br/pagar"
        )
        send_whatsapp_message(phone_clean, msg, db)
        db.add(CrmMessage(
            conversation_id=conv.id,
            direction="out",
            content=msg,
            sent_by="ai",
        ))
        conv.updated_at = datetime.utcnow()
        db.commit()
    except Exception:
        pass
    finally:
        db.close()


def _ativar_no_crm(phone: str, db):
    """Muda stage para active no CRM quando pagamento é aprovado."""
    from models import CrmConversation, CrmMessage
    from datetime import datetime

    if not phone:
        return
    phone_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    conv = db.query(CrmConversation).filter(CrmConversation.phone == phone_clean).first()
    if conv and conv.stage not in ("active",):
        conv.stage = "active"
        conv.followup_count = 0  # zera follow-up — não é mais lead
        conv.last_followup_at = None
        conv.updated_at = datetime.utcnow()
        db.add(CrmMessage(
            conversation_id=conv.id,
            direction="out",
            content="[Sistema] Pagamento aprovado — licença ativada.",
            sent_by="system",
        ))
        db.commit()


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
async def create_pix(email: str, plano: str, whatsapp: str = "", nome: str = "", db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if user and plano == "mensal" and user.plan_type == "mensal":
        return {"error": "Plano mensal já utilizado"}

    # Salva usuário e já entra no CRM — independente do PIX funcionar
    if whatsapp:
        if not user:
            from models import User as UserModel
            user = UserModel(email=email, whatsapp=whatsapp, nome=nome or None)
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            if not user.whatsapp:
                user.whatsapp = whatsapp
            if nome and not user.nome:
                user.nome = nome
            db.commit()

        # Cria/atualiza conversa no CRM para o follow-up automático
        _registrar_lead_crm(whatsapp, email, plano, db, nome=nome)

    valor = 99.00 if plano == "mensal" else 499.00 if plano == "anual" else None
    if valor is None:
        return {"error": "Plano inválido"}

    try:
        resultado = criar_pix(email, valor, plano)
    except Exception as e:
        return {"error": f"Falha ao gerar PIX: {e}"}

    if not resultado:
        return {"error": "Falha ao gerar PIX"}

    # 5 min: se não pagou, Maia entra em contato
    if whatsapp:
        asyncio.create_task(_abandono_pix(str(resultado.get("id")), whatsapp))

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
            licenca_ativada_agora = False
            if user and (sem_licenca or expirada):
                dias = 30 if plano == "mensal" else 365
                user.expires_at = datetime.utcnow() + timedelta(days=dias)
                user.plan_type  = plano
                if not user.password:
                    user.pre_liberado = True
                db.commit()
                licenca_ativada_agora = True

            # Atualiza CRM para active
            phone = user.whatsapp if user else None
            _ativar_no_crm(phone, db)

            # Envia notificações só uma vez (quando licença for ativada agora)
            if licenca_ativada_agora and user:
                from services.whatsapp_service import send_whatsapp_message
                plano_nome = "Mensal" if plano == "mensal" else "Anual"
                # Mensagem para o cliente
                if user.whatsapp:
                    try:
                        msg_cliente = (
                            f"✅ *Pagamento confirmado!*\n\n"
                            f"Olá, {user.nome or email}!\n\n"
                            f"Seu plano *Guardian Shield {plano_nome}* foi ativado com sucesso.\n\n"
                            f"📥 *Baixe o aplicativo pelo link abaixo:*\n"
                            f"https://drive.google.com/uc?export=download&id=1IF5gPconoMyfDU8HKLPIaMGlHu5UaIL4\n\n"
                            f"Após instalar, abra o app, clique em *Cadastro*, use o e-mail acima e crie sua senha. Em seguida verifique seu WhatsApp para ativar o acesso.\n\n"
                            f"Qualquer dúvida, é só chamar! 🛡️"
                        )
                        send_whatsapp_message(user.whatsapp, msg_cliente, db)
                    except Exception:
                        pass
                # Notificação para o dono
                try:
                    plano_label = "Mensal (R$99)" if plano == "mensal" else "Anual (R$499)"
                    msg_dono = (
                        f"🔔 *Nova venda Guardian Shield!*\n\n"
                        f"💰 PIX — Plano: *{plano_label}*\n"
                        f"📧 Cliente: {email}\n"
                        f"📱 WhatsApp: {user.whatsapp or 'não informado'}\n\n"
                        f"✅ Licença ativada automaticamente."
                    )
                    send_whatsapp_message("45998452596", msg_dono, db)
                except Exception:
                    pass

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
    whatsapp           = data.get("whatsapp", "")
    nome               = data.get("nome", "")
    token              = data.get("token")
    installments       = data.get("installments", 1)
    payment_method_id  = data.get("payment_method_id")
    issuer_id          = data.get("issuer_id")

    if not token or not payment_method_id:
        return {"error": "Dados de cartão inválidos"}

    valor = 99.00 if plano == "mensal" else 499.00

    # Salva WhatsApp do lead e registra no CRM antes do pagamento
    if whatsapp and email:
        _db = SessionLocal()
        try:
            _u = _db.query(User).filter(User.email == email).first()
            if _u:
                if not _u.whatsapp:
                    _u.whatsapp = whatsapp
                if nome and not _u.nome:
                    _u.nome = nome
                _db.commit()
            else:
                _u = User(email=email, whatsapp=whatsapp, nome=nome or None)
                _db.add(_u)
                _db.commit()
            _registrar_lead_crm(whatsapp, email, plano, _db, nome=nome)
        finally:
            _db.close()

    try:
        resultado = processar_cartao(email, valor, plano, token, installments, payment_method_id, issuer_id)
        status = resultado.get("status")

        # Cartão negado — avisa o cliente via WhatsApp
        if status in ("rejected", "cc_rejected_other_reason", "cc_rejected_insufficient_amount",
                      "cc_rejected_bad_filled_card_number", "cc_rejected_bad_filled_date",
                      "cc_rejected_bad_filled_security_code") and whatsapp:
            try:
                _db = SessionLocal()
                from services.whatsapp_service import send_whatsapp_message
                msg = (
                    f"❌ *Pagamento não aprovado*\n\n"
                    f"Infelizmente seu cartão foi recusado.\n\n"
                    f"Você pode tentar novamente com outro cartão ou pagar via PIX:\n"
                    f"👉 https://guardian.grupomayconsantos.com.br/pagar\n\n"
                    f"Qualquer dúvida é só responder aqui. 🛡️"
                )
                send_whatsapp_message(whatsapp, msg, _db)
                _db.close()
            except Exception:
                pass

        # Cartão aprovado — ativa licença, confirma para o cliente e notifica o dono
        if status == "approved":
            _db2 = SessionLocal()
            try:
                from datetime import timedelta
                from services.whatsapp_service import send_whatsapp_message
                plano_nome = "Mensal" if plano == "mensal" else "Anual"
                dias = 30 if plano == "mensal" else 365

                # Ativa licença
                user_db = _db2.query(User).filter(User.email == email).first()
                if user_db:
                    user_db.expires_at = __import__('datetime').datetime.utcnow() + timedelta(days=dias)
                    user_db.plan_type  = plano
                    if not user_db.password:
                        user_db.pre_liberado = True
                    _db2.commit()

                # Mensagem de confirmação para o cliente
                if whatsapp:
                    nome_cliente = (user_db.nome if user_db and user_db.nome else email)
                    msg_cliente = (
                        f"✅ *Pagamento confirmado!*\n\n"
                        f"Olá, {nome_cliente}!\n\n"
                        f"Seu plano *Guardian Shield {plano_nome}* foi ativado com sucesso.\n\n"
                        f"📥 *Baixe o aplicativo pelo link abaixo:*\n"
                        f"https://drive.google.com/uc?export=download&id=1IF5gPconoMyfDU8HKLPIaMGlHu5UaIL4\n\n"
                        f"Após instalar, abra o app, clique em *Cadastro*, use o e-mail acima e crie sua senha. Em seguida verifique seu WhatsApp para ativar o acesso.\n\n"
                        f"Qualquer dúvida, é só chamar! 🛡️"
                    )
                    send_whatsapp_message(whatsapp, msg_cliente, _db2)

                # Notificação para o dono
                plano_label = "Mensal (R$99)" if plano == "mensal" else "Anual (R$499)"
                msg_dono = (
                    f"🔔 *Nova venda Guardian Shield!*\n\n"
                    f"💳 Cartão — Plano: *{plano_label}*\n"
                    f"📧 Cliente: {email}\n"
                    f"📱 WhatsApp: {whatsapp or 'não informado'}\n\n"
                    f"✅ Licença ativada automaticamente."
                )
                send_whatsapp_message("45998452596", msg_dono, _db2)
                # Atualiza CRM para active
                _ativar_no_crm(whatsapp, _db2)
            except Exception:
                pass
            finally:
                _db2.close()

        return {"status": status, "payment_id": resultado.get("id")}
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
