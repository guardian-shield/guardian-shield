import os
import asyncio
import logging
from fastapi import APIRouter, Depends, Request

logger = logging.getLogger("guardian")
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User
from payment import criar_pix, criar_pagamento, buscar_pagamento, processar_cartao

router = APIRouter()

# Armazena contexto do browser (fbc, fbp, ip, ua) por payment_id do PIX
# para enriquecer o CAPI no momento da confirmação (webhook assíncrono)
_pix_meta_ctx: dict = {}


def _registrar_conversao_afiliado(db, slug: str, email_cliente: str, nome_cliente: str,
                                   whatsapp_cliente: str, plano: str, valor_cents: int,
                                   payment_id: str = "", metodo: str = "pix"):
    """Registra conversão e notifica o afiliado via WhatsApp."""
    from models import Affiliate, AffiliateConversion
    from services.whatsapp_service import send_whatsapp_message

    aff = db.query(Affiliate).filter(Affiliate.slug == slug, Affiliate.ativo == True).first()
    if not aff:
        return

    comissao_cents = int(valor_cents * aff.comissao_pct / 100)

    db.add(AffiliateConversion(
        affiliate_slug   = slug,
        email_cliente    = email_cliente,
        nome_cliente     = nome_cliente or email_cliente,
        whatsapp_cliente = whatsapp_cliente,
        plano            = plano,
        valor            = valor_cents,
        comissao         = comissao_cents,
        payment_id       = payment_id,
        metodo           = metodo,
    ))
    db.commit()

    if aff.whatsapp:
        valor_fmt    = f"R${valor_cents/100:.2f}".replace(".", ",")
        comissao_fmt = f"R${comissao_cents/100:.2f}".replace(".", ",")
        try:
            send_whatsapp_message(
                aff.whatsapp,
                f"🎉 *Nova venda pelo seu link!*\n\n"
                f"👤 Cliente: {nome_cliente or email_cliente}\n"
                f"💰 Valor: *{valor_fmt}*\n"
                f"🤑 Sua comissão: *{comissao_fmt}* ({aff.comissao_pct}%)\n\n"
                f"Acesse seu painel para acompanhar:\n"
                f"https://guardian.grupomayconsantos.com.br/afiliado/{slug}/painel",
                db,
            )
        except Exception:
            pass


# Preços por plano em centavos — fonte única de verdade para fallback
PLANO_PRECOS_CENTS = {
    "mensal":    9900,   # R$99,00  (app)
    "anual":    29900,   # R$299,00 (web PIX padrão) — app pode cobrar R$399 via checkout
    "anual99":   9900,   # R$99,00  (promo web — legado)
    "anual147": 14700,   # R$147,00 (promo web)
    "anual79":   7990,   # R$79,90  (promo web)
    "anual199": 19900,   # R$199,00 (promo web)
    "teste":     4990,   # R$49,90  (trial pago)
}


def _registrar_pagamento_db(db, email: str, plano: str, valor_cents: int,
                             payment_id: str, metodo: str = "pix",
                             afiliado_slug: str = None) -> bool:
    """Registra transação na tabela pagamentos com deduplicação por payment_id.

    Retorna:
      True  — inseriu agora (primeira vez) → pode ativar e notificar.
      False — duplicata confirmada (IntegrityError / já existia no SELECT).
              → não ativar nem notificar.

    Regra de ouro: qualquer falha que NÃO seja duplicata confirmada retorna True,
    porque o pior resultado é o cliente pagar e não receber nada.
    Só bloqueamos quando temos PROVA de que é repetição."""
    import logging
    from sqlalchemy.exc import IntegrityError
    from models import Pagamento

    if not payment_id:
        return True  # sem ID não conseguimos deduplicar — entrega na dúvida

    try:
        existe = db.query(Pagamento).filter(Pagamento.payment_id == str(payment_id)).first()
        if existe:
            return False  # duplicata confirmada via SELECT
        db.add(Pagamento(
            email         = email,
            plano         = plano,
            valor_cents   = valor_cents,
            payment_id    = str(payment_id),
            metodo        = metodo,
            afiliado_slug = afiliado_slug or None,
        ))
        db.commit()
        return True

    except IntegrityError as _ie:
        # UNIQUE constraint estourou — prova de duplicata em corrida (webhook + polling)
        db.rollback()
        logging.getLogger("guardian").warning(
            f"[PAGAMENTO] payment_id {payment_id} duplicado (IntegrityError) — bloqueando reprocessamento"
        )
        return False

    except Exception as _e:
        # Falha genérica (timeout, conexão, etc.) — na dúvida, entrega
        db.rollback()
        logging.getLogger("guardian").error(
            f"[PAGAMENTO] Falha ao registrar transação (payment_id={payment_id}): {_e} — seguindo em frente"
        )
        return True


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
async def create_pix(request: Request, email: str, plano: str, whatsapp: str = "", nome: str = "", afiliado: str = "", fbc: str = "", fbp: str = "", db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if user and plano == "teste" and user.trial_usado:
        return {"error": "O período de teste já foi utilizado nesta conta. Para continuar usando o Guardian Shield, adquira o plano anual."}

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

    import re as _re
    if not email or not _re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', email):
        return {"error": "E-mail inválido. Verifique e tente novamente."}

    valor = 49.90 if plano == "teste" else 79.90 if plano == "anual79" else 99.00 if plano == "anual99" else 147.00 if plano == "anual147" else 199.00 if plano == "anual199" else 299.00 if plano == "anual" else None
    if valor is None:
        return {"error": "Plano inválido"}

    ext_ref = f"{email}|{plano}"
    if afiliado:
        ext_ref += f"|{afiliado}"

    try:
        resultado = criar_pix(email, valor, plano, external_reference=ext_ref)
    except Exception as e:
        return {"error": f"Falha ao gerar PIX: {e}"}

    if not resultado:
        return {"error": "Falha ao gerar PIX"}

    # Verifica se o MP retornou erro (status diferente de pending/approved)
    resultado_status = resultado.get("status")
    if resultado_status not in ("pending", "approved", None):
        err_msg = resultado.get("message") or resultado.get("error") or "Falha ao gerar PIX"
        logger.warning(f"[PIX] MP retornou status inesperado: {resultado_status} — {err_msg} — email={email}")
        return {"error": f"Falha ao gerar PIX: {err_msg}"}

    pix = resultado.get("point_of_interaction", {}).get("transaction_data", {})

    # MP às vezes cria o pagamento mas demora para gerar o QR code.
    # Se temos payment_id mas não temos qr_code_base64, busca o pagamento já criado
    # (sem criar um novo) — tenta até 3x com 1s de espera entre tentativas.
    payment_id_criado = resultado.get("id")
    if payment_id_criado and (not pix.get("qr_code_base64") or not pix.get("qr_code")):
        logger.warning(f"[PIX] qr_code ausente na criação — payment_id={payment_id_criado} status={resultado_status} — tentando buscar... email={email}")
        for tentativa in range(3):
            await asyncio.sleep(1)
            buscado = buscar_pagamento(payment_id_criado)
            if buscado:
                pix_buscado = buscado.get("point_of_interaction", {}).get("transaction_data", {})
                if pix_buscado.get("qr_code_base64") and pix_buscado.get("qr_code"):
                    logger.warning(f"[PIX] qr_code obtido na tentativa {tentativa+1} — payment_id={payment_id_criado}")
                    pix = pix_buscado
                    resultado = buscado
                    break
                logger.warning(f"[PIX] Tentativa {tentativa+1}/3 — qr_code ainda ausente — payment_id={payment_id_criado}")

    if not pix.get("qr_code_base64") or not pix.get("qr_code"):
        err_msg = resultado.get("message") or resultado.get("error") or "QR Code não gerado pelo Mercado Pago"
        logger.warning(f"[PIX] qr_code não disponível após 3 tentativas — payment_id={payment_id_criado} — {err_msg} — email={email}")
        return {"error": f"PIX não gerado: {err_msg}"}

    # Registra PIX pendente para reconciliação periódica
    # (garante ativação mesmo se browser fechar antes do pagamento ser aprovado)
    try:
        from models import PendingPix
        _pid_str = str(payment_id_criado) if payment_id_criado else ""
        if _pid_str:
            db.add(PendingPix(payment_id=_pid_str, email=email, plano=plano, afiliado_slug=afiliado or None))
            db.commit()
    except Exception:
        db.rollback()  # não bloqueia o PIX por falha no tracking

    # Cria fila de recuperação de abandono (substitui o task antigo)
    if whatsapp:
        try:
            from services.recovery_service import criar_fila_abandono
            criar_fila_abandono(
                phone=whatsapp.replace("+", "").replace(" ", "").replace("-", ""),
                email=email,
                nome=nome,
            )
        except Exception as _e:
            logger.warning(f"[PAGAMENTO] Erro ao criar fila abandono: {_e}")
            # Fallback para o task antigo se recovery_service falhar
            asyncio.create_task(_abandono_pix(str(resultado.get("id")), whatsapp))

    # Salva contexto do browser para enriquecer o CAPI na confirmação
    _pid = str(resultado.get("id", ""))
    if _pid:
        _pix_meta_ctx[_pid] = {
            "fbc": fbc or None,
            "fbp": fbp or None,
            "ip":  request.client.host if request.client else None,
            "ua":  request.headers.get("user-agent"),
        }

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
        # Suporta separador "|" (novo) e "-" (legado); 3 partes = email|plano|afiliado
        if reference and "|" in reference:
            parts = reference.split("|")
        elif reference and "-" in reference:
            parts = reference.split("-", 1)
        else:
            parts = []
        email          = parts[0] if parts else pagamento.get("payer", {}).get("email")
        plano          = parts[1] if len(parts) > 1 else "teste"
        afiliado_slug  = parts[2] if len(parts) > 2 else None

        if email:
            # ── Porteiro de deduplicação — mesma proteção do webhook ──
            # Garante que webhook + polling não ativem o mesmo payment_id duas vezes.
            tx_real_pre      = pagamento.get("transaction_amount") or 0
            planoValor_pre   = {"teste": 49.90, "anual79": 79.90, "anual99": 99.00,
                                 "anual147": 147.00, "anual199": 199.00}.get(plano, 299.00)
            valor_cents_pre  = int(round(float(tx_real_pre) * 100)) if tx_real_pre else int(planoValor_pre * 100)
            _inseriu = _registrar_pagamento_db(
                db            = db,
                email         = email,
                plano         = plano,
                valor_cents   = valor_cents_pre,
                payment_id    = str(payment_id),
                metodo        = "pix",
                afiliado_slug = afiliado_slug,
            )
            if not _inseriu:
                logger.warning(f"[PIX_STATUS] payment_id {payment_id} já processado — pulando ativação")
                return {"status": "approved", "already_processed": True}

            user = db.query(User).filter(User.email == email).first()
            # Cria usuário se não existir (ex: checkout não chamou /create-pix antes)
            if not user:
                user = User(email=email)
                db.add(user)
                db.commit()
                db.refresh(user)
            sem_licenca = not user.expires_at
            expirada    = (user.expires_at is not None and user.expires_at < datetime.utcnow())
            # Upgrade de trial para plano pago — ativa mesmo com trial ainda válido
            upgrade_trial = (
                plano not in ("teste", "trial_gratis") and
                user.plan_type in ("trial_gratis", None)
            )
            licenca_ativada_agora = False
            if sem_licenca or expirada or upgrade_trial:
                dias = 30 if plano == "teste" else 365
                _base = max(datetime.utcnow(), user.expires_at or datetime.utcnow())
                user.expires_at = _base + timedelta(days=dias)
                user.plan_type  = "anual" if plano in ("anual79", "anual99", "anual147", "anual199") else plano
                if plano == "teste":
                    user.trial_usado = True
                if not user.password:
                    user.pre_liberado = True
                db.commit()
                licenca_ativada_agora = True

            # Atualiza CRM para active
            phone = user.whatsapp if user else None
            _ativar_no_crm(phone, db)

            # Cancela fila de abandono e cria suporte pós-pagamento
            if phone and licenca_ativada_agora:
                try:
                    from services.recovery_service import cancelar_fila, criar_fila_suporte
                    phone_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
                    cancelar_fila(phone_clean, tipo="abandonment")
                    criar_fila_suporte(phone_clean, email=email, nome=user.nome or "")
                except Exception as _e:
                    pass

            # Envia notificações só uma vez (quando licença for ativada agora)
            if licenca_ativada_agora and user:
                from services.whatsapp_service import send_whatsapp_message
                plano_nome = "Teste 30 dias" if plano == "teste" else "Anual"
                plano_label_pix = "Teste 30 dias (R$49,90)" if plano == "teste" else "Anual Especial (R$79,90)" if plano == "anual79" else "Anual Promo (R$99)" if plano == "anual99" else "Anual Promo (R$147)" if plano == "anual147" else "Anual Exclusiva (R$199)" if plano == "anual199" else "Anual (R$299)"
                planoValor_pix = 49.90 if plano == "teste" else 79.90 if plano == "anual79" else 99.00 if plano == "anual99" else 147.00 if plano == "anual147" else 199.00 if plano == "anual199" else 299.00
                # Mensagem para o cliente
                if user.whatsapp:
                    try:
                        msg_cliente = (
                            f"✅ *Pagamento confirmado!*\n\n"
                            f"Olá, {user.nome or email}!\n\n"
                            f"Seu plano *Guardian Shield {plano_nome}* foi ativado com sucesso.\n\n"
                            f"📥 *Baixe o aplicativo:*\n"
                            f"https://github.com/grupoempresarialmayconsantos-bot/guardian-releases/releases/latest/download/Guardian-Shield-Setup.exe\n\n"
                            f"Após instalar, faça login com seu e-mail e verifique o WhatsApp para ativar o acesso.\n\n"
                            f"🎥 *Tutorial completo (conectar, scan, blindagem e certificado):*\n"
                            f"https://www.youtube.com/watch?v=92dTghZ8RQc\n\n"
                            f"Qualquer dúvida, é só chamar! 🛡️"
                        )
                        send_whatsapp_message(user.whatsapp, msg_cliente, db)
                    except Exception:
                        pass
                # Notificação para o dono
                try:
                    msg_dono = (
                        f"🔔 *Nova venda Guardian Shield!*\n\n"
                        f"💰 PIX — Plano: *{plano_label_pix}*\n"
                        f"📧 Cliente: {email}\n"
                        f"📱 WhatsApp: {user.whatsapp or 'não informado'}\n\n"
                        f"✅ Licença ativada automaticamente."
                    )
                    send_whatsapp_message("45998452596", msg_dono, db)
                    logger.warning(f"[VENDA] Notificação enviada ao dono — PIX {plano_label_pix} {email}")
                except Exception as e:
                    logger.error(f"[VENDA] FALHA ao notificar dono — PIX {email}: {e}")

                # Meta Conversions API — Purchase server-side (PIX)
                try:
                    from services.meta_events import send_purchase
                    _pid_pix = str(pagamento.get("id", ""))
                    _ctx = _pix_meta_ctx.pop(_pid_pix, {})
                    send_purchase(
                        email=email,
                        valor=planoValor_pix,
                        plano=plano,
                        event_id=f"purchase-pix-{_pid_pix}",
                        phone=user.whatsapp,
                        external_id=user.id,
                        fbc=_ctx.get("fbc"),
                        fbp=_ctx.get("fbp"),
                        client_ip=_ctx.get("ip"),
                        client_ua=_ctx.get("ua"),
                    )
                except Exception as _me:
                    logger.warning(f"[META] Falha ao enviar Purchase PIX: {_me}")

                # Conversão de afiliado
                if afiliado_slug:
                    _registrar_conversao_afiliado(
                        db=db,
                        slug=afiliado_slug,
                        email_cliente=email,
                        nome_cliente=user.nome or "",
                        whatsapp_cliente=user.whatsapp or "",
                        plano=plano,
                        valor_cents=int(planoValor_pix * 100),
                        payment_id=str(pagamento.get("id", "")),
                        metodo="pix",
                    )

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
    plano              = data.get("plano", "teste")
    whatsapp           = data.get("whatsapp", "")
    nome               = data.get("nome", "")
    afiliado_slug_card = data.get("afiliado", "")
    token              = data.get("token")
    installments       = data.get("installments", 1)
    payment_method_id  = data.get("payment_method_id")
    issuer_id          = data.get("issuer_id")
    identification     = data.get("payer", {}).get("identification") or {}

    if not token or not payment_method_id:
        return {"error": "Dados de cartão inválidos"}

    # Bloqueia trial se já foi usado
    _db_check = SessionLocal()
    try:
        _u_check = _db_check.query(User).filter(User.email == email).first()
        if _u_check and plano == "teste" and _u_check.trial_usado:
            return {"error": "O período de teste já foi utilizado nesta conta. Para continuar usando o Guardian Shield, adquira o plano anual."}
    finally:
        _db_check.close()

    valor = 49.90 if plano == "teste" else 79.90 if plano == "anual79" else 99.00 if plano == "anual99" else 147.00 if plano == "anual147" else 199.00 if plano == "anual199" else 299.00

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
        resultado = processar_cartao(email, valor, plano, token, installments, payment_method_id, issuer_id, identification)
        status = resultado.get("status")
        status_detail = resultado.get("status_detail", "")
        logger.warning(f"[CARTÃO] status={status} detail={status_detail} email={email} plano={plano}")

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
                plano_nome = "Teste 30 dias" if plano == "teste" else "Anual"
                dias = 30 if plano == "teste" else 365

                # Ativa licença
                from datetime import datetime as _dt
                user_db = _db2.query(User).filter(User.email == email).first()
                if not user_db:
                    user_db = User(
                        email        = email,
                        whatsapp     = whatsapp or None,
                        nome         = nome or None,
                        pre_liberado = True,
                        expires_at   = _dt.utcnow() + timedelta(days=dias),
                        plan_type    = "anual" if plano in ("anual79", "anual99", "anual147", "anual199") else plano,
                    )
                    _db2.add(user_db)
                    _db2.commit()
                    _db2.refresh(user_db)
                else:
                    _base_card = max(_dt.utcnow(), user_db.expires_at or _dt.utcnow())
                    user_db.expires_at = _base_card + timedelta(days=dias)
                    user_db.plan_type  = "anual" if plano in ("anual79", "anual99", "anual147", "anual199") else plano
                    if plano == "teste":
                        user_db.trial_usado = True
                    if not user_db.password:
                        user_db.pre_liberado = True
                    _db2.commit()

                # Cancela abandono e inicia suporte pós-pagamento
                if whatsapp:
                    try:
                        from services.recovery_service import cancelar_fila, criar_fila_suporte
                        phone_clean = whatsapp.replace("+", "").replace(" ", "").replace("-", "")
                        cancelar_fila(phone_clean, tipo="abandonment")
                        criar_fila_suporte(phone_clean, email=email, nome=nome)
                    except Exception:
                        pass

                # Mensagem de confirmação para o cliente
                if whatsapp:
                    nome_cliente = (user_db.nome if user_db and user_db.nome else email)
                    msg_cliente = (
                        f"✅ *Pagamento confirmado!*\n\n"
                        f"Olá, {nome_cliente}!\n\n"
                        f"Seu plano *Guardian Shield {plano_nome}* foi ativado com sucesso.\n\n"
                        f"📥 *Baixe o aplicativo:*\n"
                        f"https://github.com/grupoempresarialmayconsantos-bot/guardian-releases/releases/latest/download/Guardian-Shield-Setup.exe\n\n"
                        f"Após instalar, faça login com seu e-mail e verifique o WhatsApp para ativar o acesso.\n\n"
                        f"🎥 *Tutorial completo (conectar, scan, blindagem e certificado):*\n"
                        f"https://www.youtube.com/watch?v=92dTghZ8RQc\n\n"
                        f"Qualquer dúvida, é só chamar! 🛡️"
                    )
                    send_whatsapp_message(whatsapp, msg_cliente, _db2)

                # Notificação para o dono
                plano_label_card = "Teste 30 dias (R$49,90)" if plano == "teste" else "Anual Especial (R$79,90)" if plano == "anual79" else "Anual Promo (R$99)" if plano == "anual99" else "Anual Promo (R$147)" if plano == "anual147" else "Anual Exclusiva (R$199)" if plano == "anual199" else "Anual (R$299)"
                msg_dono = (
                    f"🔔 *Nova venda Guardian Shield!*\n\n"
                    f"💳 Cartão — Plano: *{plano_label_card}*\n"
                    f"📧 Cliente: {email}\n"
                    f"📱 WhatsApp: {whatsapp or 'não informado'}\n\n"
                    f"✅ Licença ativada automaticamente."
                )
                try:
                    send_whatsapp_message("45998452596", msg_dono, _db2)
                    logger.warning(f"[VENDA] Notificação enviada ao dono — Cartão {plano_label_card} {email}")
                except Exception as e:
                    logger.error(f"[VENDA] FALHA ao notificar dono — Cartão {email}: {e}")
                # Atualiza CRM para active
                _ativar_no_crm(whatsapp, _db2)

                # Registra transação para dashboard de receita
                tx_real_card = resultado.get("transaction_amount") or 0
                valor_card_cents = int(round(float(tx_real_card) * 100)) if tx_real_card else int(valor * 100)
                _registrar_pagamento_db(
                    db=_db2,
                    email=email,
                    plano=plano,
                    valor_cents=valor_card_cents,
                    payment_id=str(resultado.get("id", "")),
                    metodo="cartao",
                    afiliado_slug=afiliado_slug_card or None,
                )

                # Meta Conversions API — Purchase server-side (Cartão)
                try:
                    from services.meta_events import send_purchase
                    _fbc_card = data.get("fbc") or None
                    _fbp_card = data.get("fbp") or None
                    send_purchase(
                        email=email,
                        valor=valor,
                        plano=plano,
                        event_id=f"purchase-card-{resultado.get('id', '')}",
                        phone=whatsapp,
                        external_id=user_db.id if user_db else None,
                        fbc=_fbc_card,
                        fbp=_fbp_card,
                        client_ip=request.client.host if request.client else None,
                        client_ua=request.headers.get("user-agent"),
                    )
                except Exception as _me:
                    logger.warning(f"[META] Falha ao enviar Purchase Cartão: {_me}")

                # Conversão de afiliado (cartão)
                if afiliado_slug_card:
                    _registrar_conversao_afiliado(
                        db=_db2,
                        slug=afiliado_slug_card,
                        email_cliente=email,
                        nome_cliente=nome or (user_db.nome if user_db else ""),
                        whatsapp_cliente=whatsapp,
                        plano=plano,
                        valor_cents=int(valor * 100),
                        payment_id=str(resultado.get("id", "")),
                        metodo="cartao",
                    )
            except Exception:
                pass
            finally:
                _db2.close()

        REJECTION_MESSAGES = {
            "cc_rejected_insufficient_amount":      "Saldo insuficiente no cartão.",
            "cc_rejected_bad_filled_card_number":   "Número do cartão inválido.",
            "cc_rejected_bad_filled_date":          "Data de vencimento inválida.",
            "cc_rejected_bad_filled_security_code": "Código de segurança inválido.",
            "cc_rejected_call_for_authorize":       "Cartão bloqueado. Ligue para o banco para autorizar.",
            "cc_rejected_card_disabled":            "Cartão desativado. Contate seu banco.",
            "cc_rejected_duplicated_payment":       "Pagamento duplicado detectado.",
            "cc_rejected_high_risk":                "Transação recusada por segurança. Tente outro cartão.",
            "cc_rejected_other_reason":             "Cartão recusado pelo banco. Tente outro cartão ou use o PIX.",
        }
        if status == "rejected":
            msg = REJECTION_MESSAGES.get(status_detail, "Cartão recusado. Tente outro cartão ou pague via PIX.")
            return {"status": "rejected", "error": msg, "payment_id": resultado.get("id")}

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


# GET /vendas2  →  versão sem VSL para teste
# =============================================================
@router.get("/vendas2", response_class=HTMLResponse)
def pagina_vendas2():
    html_path = os.path.join(os.path.dirname(__file__), "..", "templates", "vendas2.html")
    with open(os.path.abspath(html_path), encoding="utf-8") as f:
        return f.read()


# GET /vendas3  →  oferta especial anual R$79,90
# =============================================================
@router.get("/vendas3", response_class=HTMLResponse)
def pagina_vendas3():
    html_path = os.path.join(os.path.dirname(__file__), "..", "templates", "vendas3.html")
    with open(os.path.abspath(html_path), encoding="utf-8") as f:
        return f.read()


# GET /vendas4  →  teste grátis 7 dias (Instagram)
# =============================================================
@router.get("/vendas4", response_class=HTMLResponse)
def pagina_vendas4():
    html_path = os.path.join(os.path.dirname(__file__), "..", "templates", "vendas4.html")
    with open(os.path.abspath(html_path), encoding="utf-8") as f:
        return f.read()


# GET /vendas5  →  página de vendas anual R$297
# =============================================================
@router.get("/vendas5", response_class=HTMLResponse)
def pagina_vendas5():
    html_path = os.path.join(os.path.dirname(__file__), "..", "templates", "vendas5.html")
    with open(os.path.abspath(html_path), encoding="utf-8") as f:
        return f.read()


# GET /vendas6  →  página de vendas por lotes — Lote 2 R$199
# =============================================================
@router.get("/vendas6", response_class=HTMLResponse)
def pagina_vendas6():
    html_path = os.path.join(os.path.dirname(__file__), "..", "templates", "vendas6.html")
    with open(os.path.abspath(html_path), encoding="utf-8") as f:
        return f.read()


# POST /register-free-trial  →  cadastro gratuito 7 dias
# =============================================================
from pydantic import BaseModel as _BaseModel

class TrialRegisterRequest(_BaseModel):
    nome: str
    email: str
    whatsapp: str
    senha: str


@router.post("/register-free-trial")
async def register_free_trial(body: TrialRegisterRequest, db: Session = Depends(get_db)):
    from datetime import datetime, timedelta
    from auth import hash_password

    nome     = body.nome.strip()
    email    = body.email.strip().lower()
    whatsapp = body.whatsapp.strip().replace("+", "").replace(" ", "").replace("-", "")
    senha    = body.senha.strip()

    if not nome or not email or not whatsapp or not senha:
        return {"error": "Preencha todos os campos."}
    if "@" not in email:
        return {"error": "E-mail inválido."}
    if len(whatsapp) < 10:
        return {"error": "WhatsApp inválido."}
    if len(senha) < 6:
        return {"error": "A senha deve ter pelo menos 6 caracteres."}

    # Verifica se email já existe
    existente = db.query(User).filter(User.email == email).first()
    if existente:
        # Se já tem licença ativa, informa
        if existente.expires_at and existente.expires_at > datetime.utcnow():
            return {"error": "Este e-mail já tem uma conta ativa. Faça login no aplicativo."}
        # Se nunca usou trial, ativa
        if existente.trial_usado:
            return {"error": "Este e-mail já utilizou o período de teste. Para continuar, adquira o plano anual."}

    senha_hash = hash_password(senha)

    if existente:
        user = existente
        if not user.password:
            user.password = senha_hash
        if not user.whatsapp:
            user.whatsapp = whatsapp
        if not user.nome:
            user.nome = nome
    else:
        user = User(
            nome=nome,
            email=email,
            password=senha_hash,
            whatsapp=whatsapp,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    # Ativa licença de 7 dias
    user.expires_at  = datetime.utcnow() + timedelta(days=7)
    user.plan_type   = "trial_gratis"
    user.trial_usado = True
    # pre_liberado=False — usuário já tem senha, pode fazer login normalmente
    user.pre_liberado = False
    db.commit()

    # CRM — cria conversa como "active" (tem acesso) para IA saber que é trial
    from models import CrmConversation, CrmMessage
    conv = db.query(CrmConversation).filter(CrmConversation.phone == whatsapp).first()
    if not conv:
        conv = CrmConversation(
            phone=whatsapp,
            contact_name=nome,
            contact_email=email,
            stage="active",
            ai_active=True,
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)
    else:
        conv.stage = "active"
        conv.contact_name  = conv.contact_name or nome
        conv.contact_email = conv.contact_email or email
        db.commit()

    db.add(CrmMessage(
        conversation_id=conv.id,
        direction="out",
        content=f"[Sistema] Cadastro via teste grátis (vendas4) — 7 dias. Plano: trial_gratis.",
        sent_by="system",
    ))
    db.commit()

    # WhatsApp de boas-vindas
    try:
        from services.whatsapp_service import send_whatsapp_message
        msg_boas_vindas = (
            f"🎉 *Olá, {nome}!*\n\n"
            f"Seu acesso ao *Guardian Shield* foi ativado — você tem *7 dias grátis* para testar tudo.\n\n"
            f"📥 *Baixe agora pelo link:*\n"
            f"https://guardian.grupomayconsantos.com.br/download\n\n"
            f"Após instalar, abra o app, clique em *Login* e entre com:\n"
            f"📧 {email}\n\n"
            f"🎥 *Tutorial completo (conectar o celular, scan, blindagem e certificado):*\n"
            f"https://www.youtube.com/watch?v=92dTghZ8RQc\n\n"
            f"Qualquer dúvida é só responder aqui — estou aqui para te ajudar! 🛡️"
        )
        send_whatsapp_message(whatsapp, msg_boas_vindas, db)
    except Exception:
        pass

    # Notificação do dono agora é feita em lote às 12h e 20h (crm_followup.py)
    # para não spammar a cada cadastro individual

    # Inicia fila de nurturing (conversão) + fila de ativação (quem não baixou)
    try:
        from services.recovery_service import criar_fila_trial_nurture, criar_fila_trial_ativacao
        criar_fila_trial_nurture(whatsapp, email=email, nome=nome)
        criar_fila_trial_ativacao(whatsapp, email=email, nome=nome)
    except Exception:
        pass

    # Conversions API — Purchase com valor 0 para o Meta registrar custo por cadastro
    import time as _time
    event_id = f"trial-{email}-{int(_time.time())}"
    try:
        from services.meta_events import send_purchase
        send_purchase(email=email, valor=0.00, plano="trial_gratis", event_id=event_id)
    except Exception:
        pass

    return {"ok": True, "event_id": event_id}


# =============================================================
# GET /af/{slug}  →  página de vendas do afiliado
# =============================================================
@router.get("/af/{slug}", response_class=HTMLResponse)
def pagina_afiliado(slug: str, db: Session = Depends(get_db)):
    from models import Affiliate
    aff = db.query(Affiliate).filter(Affiliate.slug == slug, Affiliate.ativo == True).first()
    if not aff:
        return HTMLResponse("<h1>Link inválido ou inativo.</h1>", status_code=404)
    html_path = os.path.join(os.path.dirname(__file__), "..", "templates", "vendas_afiliado.html")
    with open(os.path.abspath(html_path), encoding="utf-8") as f:
        html = f.read()
    return html.replace("{{SLUG}}", slug)


# =============================================================
# GET /afiliado/{slug}/painel  →  dashboard da afiliada
# =============================================================
@router.get("/afiliado/{slug}/painel", response_class=HTMLResponse)
def painel_afiliado(slug: str):
    html_path = os.path.join(os.path.dirname(__file__), "..", "templates", "afiliado_painel.html")
    with open(os.path.abspath(html_path), encoding="utf-8") as f:
        return f.read()


# =============================================================
# GET /afiliado/{slug}/dados  →  dados do painel (com senha)
# =============================================================
@router.get("/afiliado/{slug}/dados")
def dados_afiliado(slug: str, senha: str = "", db: Session = Depends(get_db)):
    import hashlib
    from models import Affiliate, AffiliateConversion

    aff = db.query(Affiliate).filter(Affiliate.slug == slug).first()
    if not aff:
        return {"error": "Afiliado não encontrado"}

    senha_hash = hashlib.sha256(senha.encode()).hexdigest()
    if aff.senha_hash != senha_hash:
        return {"error": "Senha incorreta"}

    conversoes = db.query(AffiliateConversion)\
        .filter(AffiliateConversion.affiliate_slug == slug)\
        .order_by(AffiliateConversion.created_at.desc())\
        .all()

    total_valor    = sum(c.valor for c in conversoes)
    total_comissao = sum(c.comissao for c in conversoes)

    return {
        "nome":          aff.nome or slug,
        "comissao_pct":  aff.comissao_pct,
        "total_vendas":  len(conversoes),
        "total_valor":   total_valor,
        "total_comissao": total_comissao,
        "conversoes": [
            {
                "created_at":    c.created_at.isoformat() if c.created_at else None,
                "nome_cliente":  c.nome_cliente,
                "email_cliente": c.email_cliente,
                "plano":         c.plano,
                "valor":         c.valor,
                "comissao":      c.comissao,
                "metodo":        c.metodo,
            }
            for c in conversoes
        ],
    }
