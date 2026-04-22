"""Rotas do CRM Guardian Shield."""
import os
import logging
import time
import re
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from database import SessionLocal
from models import CrmConversation, CrmMessage, AppConfig
from services.whatsapp_service import send_whatsapp_message
from services.crm_ai import get_ai_response, needs_human, clean_response, is_business_hours

logger = logging.getLogger("guardian")
router = APIRouter()


def split_message(text: str, max_len: int = 300) -> list[str]:
    """Divide o texto em partes menores em pontos naturais."""
    # Divide por parágrafos primeiro
    paragraphs = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    parts = []
    for para in paragraphs:
        if len(para) <= max_len:
            parts.append(para)
        else:
            # Divide por frases se o parágrafo for grande
            sentences = re.split(r'(?<=[.!?])\s+', para)
            chunk = ""
            for sentence in sentences:
                if len(chunk) + len(sentence) + 1 <= max_len:
                    chunk = (chunk + " " + sentence).strip()
                else:
                    if chunk:
                        parts.append(chunk)
                    chunk = sentence
            if chunk:
                parts.append(chunk)
    return parts if parts else [text]


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_cfg(db, key, default=None):
    row = db.query(AppConfig).filter(AppConfig.key == key).first()
    return row.value if row and row.value else default


# ─── Página do CRM ────────────────────────────────────────────────────────────

@router.get("/crm", response_class=HTMLResponse)
def crm_page():
    html_path = os.path.join(os.path.dirname(__file__), "..", "templates", "crm.html")
    with open(os.path.abspath(html_path), encoding="utf-8") as f:
        return f.read()


# ─── Webhook Evolution API ────────────────────────────────────────────────────

@router.post("/crm/webhook")
async def crm_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
    except Exception:
        return {"status": "ignored"}

    event = data.get("event", "")
    if event not in ("messages.upsert", "messages.update"):
        return {"status": "ignored"}

    msg_data = data.get("data", {})

    # Ignora mensagens enviadas por nós
    if msg_data.get("key", {}).get("fromMe"):
        return {"status": "ignored"}

    remote_jid = msg_data.get("key", {}).get("remoteJid", "")

    # Ignora grupos (terminam com @g.us)
    if remote_jid.endswith("@g.us"):
        return {"status": "ignored"}

    phone = remote_jid.replace("@s.whatsapp.net", "")
    if not phone:
        return {"status": "ignored"}

    wa_msg_id = msg_data.get("key", {}).get("id", "")
    content = (
        msg_data.get("message", {}).get("conversation")
        or msg_data.get("message", {}).get("extendedTextMessage", {}).get("text")
        or ""
    )
    if not content:
        return {"status": "ignored"}

    # ── Detecta mensagens automáticas / bots ─────────────────────────────────
    BOT_PATTERNS = [
        "exclusivo para o envio de informações",
        "central de atendimento",
        "mensagem automática",
        "não responda este",
        "noreply",
        "do not reply",
        "este é um número automático",
        "faq.pagbank",
        "este número não recebe mensagens",
    ]
    content_lower = content.lower()
    if any(p in content_lower for p in BOT_PATTERNS):
        logger.warning(f"[CRM] Mensagem de bot ignorada de {phone}: {content[:60]}")
        return {"status": "bot_ignored"}

    push_name = msg_data.get("pushName", "")

    # Busca ou cria conversa
    conv = db.query(CrmConversation).filter(CrmConversation.phone == phone).first()
    if not conv:
        conv = CrmConversation(phone=phone, contact_name=push_name or phone)
        db.add(conv)
        db.commit()
        db.refresh(conv)
    elif push_name and not conv.contact_name or conv.contact_name == phone:
        # Atualiza nome se ainda não tinha (era só o número)
        conv.contact_name = push_name
        db.commit()

    # Dedup — ignora mensagem já registrada
    existing = db.query(CrmMessage).filter(CrmMessage.wa_message_id == wa_msg_id).first()
    if existing:
        return {"status": "duplicate"}

    # ── Proteção anti-loop: mesma mensagem repetida 3x seguidas → ignora e desativa IA
    if conv:
        ultimas = db.query(CrmMessage)\
            .filter(CrmMessage.conversation_id == conv.id, CrmMessage.direction == "in")\
            .order_by(CrmMessage.sent_at.desc()).limit(3).all()
        if len(ultimas) >= 3 and all(m.content.strip() == content.strip() for m in ultimas):
            logger.warning(f"[CRM] Loop detectado com {phone} — desativando IA")
            conv.ai_active = False
            db.commit()
            return {"status": "loop_blocked"}

    # Salva mensagem recebida
    msg = CrmMessage(
        conversation_id=conv.id,
        direction="in",
        content=content,
        sent_by=push_name or phone,
        wa_message_id=wa_msg_id,
    )
    db.add(msg)
    conv.unread = (conv.unread or 0) + 1
    conv.updated_at = datetime.utcnow()
    db.commit()

    # ── Integração com fila de recuperação ───────────────────────────────────
    try:
        from services.recovery_service import pausar_fila, cancelar_fila, _quer_cancelar
        if _quer_cancelar(content):
            cancelar_fila(phone, db=db)
            conv.stage = "cancelled"
            conv.ai_active = False
            db.commit()
        else:
            pausar_fila(phone, db=db)
    except Exception as _re:
        logger.error(f"[CRM] Recovery integration error: {_re}")

    # Transferência manual: usuário digitou "humano"
    if content.strip().lower() == "humano":
        conv.ai_active = False
        from services.crm_ai import next_business_hours_str
        if is_business_hours():
            aviso_cliente = "Transferindo você para um atendente humano agora. Um momento! 😊"
        else:
            aviso_cliente = f"Certo! Fora do horário agora, mas nossa equipe retoma {next_business_hours_str()}. Se quiser, pode continuar falando comigo até lá! 😊"
        try:
            send_whatsapp_message(phone, aviso_cliente, db)
            db.add(CrmMessage(conversation_id=conv.id, direction="out", content=aviso_cliente, sent_by="ai"))
        except Exception:
            pass
        # Notifica o dono APENAS se estiver dentro do horário de atendimento
        if is_business_hours():
            try:
                aviso = (
                    f"🔔 *CRM — Atendimento solicitado!*\n\n"
                    f"👤 Contato: {conv.contact_name or phone}\n"
                    f"📱 WhatsApp: {phone}\n\n"
                    f"A pessoa pediu falar com atendente humano.\n"
                    f"Acesse o CRM: https://guardian.grupomayconsantos.com.br/crm"
                )
                send_whatsapp_message("45998452596", aviso, db)
            except Exception:
                pass
        db.commit()
        return {"status": "transferred"}

    # IA responde se ativa
    if conv.ai_active:
        logger.warning(f"[CRM] IA processando msg de {phone}: {content[:50]}")
        history = db.query(CrmMessage)\
            .filter(CrmMessage.conversation_id == conv.id)\
            .order_by(CrmMessage.sent_at.desc())\
            .limit(20).all()
        history = [{"direction": m.direction, "content": m.content, "sent_at": m.sent_at.isoformat() if m.sent_at else None} for m in reversed(history)]

        ai_text = get_ai_response(history, content)
        if not ai_text:
            # Cota esgotada ou resposta vazia — silencioso, não faz nada
            db.commit()
            return {"status": "quota_exceeded"}
        logger.warning(f"[CRM] Resposta IA: {ai_text[:100]}")
        transfer = needs_human(ai_text)
        reply = clean_response(ai_text)

        # Delay inicial antes de responder (comportamento humano)
        time.sleep(4)

        # Divide em partes e envia com delay entre elas
        parts = split_message(reply)
        try:
            for i, part in enumerate(parts):
                send_whatsapp_message(phone, part, db)
                if i < len(parts) - 1:
                    time.sleep(1.5)  # pausa entre partes
            logger.warning(f"[CRM] {len(parts)} parte(s) enviada(s) para {phone}")
        except Exception as e:
            logger.error(f"[CRM] Falha ao enviar resposta IA: {e}")

        # Salva resposta da IA (conteúdo completo)
        db.add(CrmMessage(
            conversation_id=conv.id,
            direction="out",
            content=reply,
            sent_by="ai",
        ))

        if transfer:
            conv.ai_active = False
            # Notifica atendente APENAS dentro do horário comercial
            if is_business_hours():
                try:
                    aviso = (
                        f"🔔 *CRM — Atendimento necessário!*\n\n"
                        f"👤 Contato: {conv.contact_name or phone}\n"
                        f"📱 WhatsApp: {phone}\n\n"
                        f"💬 Última mensagem: {content}\n\n"
                        f"Acesse o CRM para continuar: https://guardian.grupomayconsantos.com.br/crm"
                    )
                    send_whatsapp_message("45998452596", aviso, db)
                except Exception:
                    pass

        db.commit()

    return {"status": "ok"}


# ─── API: listar conversas ─────────────────────────────────────────────────────

@router.get("/crm/conversations")
def list_conversations(db: Session = Depends(get_db)):
    convs = db.query(CrmConversation).order_by(CrmConversation.updated_at.desc()).all()
    result = []
    for c in convs:
        last_msg = db.query(CrmMessage)\
            .filter(CrmMessage.conversation_id == c.id)\
            .order_by(CrmMessage.sent_at.desc()).first()
        result.append({
            "id": c.id,
            "phone": c.phone,
            "contact_name": c.contact_name,
            "contact_email": c.contact_email,
            "stage": c.stage,
            "ai_active": c.ai_active,
            "attendant": c.attendant,
            "sector": c.sector,
            "notes": c.notes,
            "unread": c.unread,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            "last_message": last_msg.content if last_msg else None,
            "last_message_direction": last_msg.direction if last_msg else None,
        })
    return result


# ─── API: mensagens de uma conversa ───────────────────────────────────────────

@router.get("/crm/conversations/{conv_id}/messages")
def get_messages(conv_id: int, db: Session = Depends(get_db)):
    conv = db.query(CrmConversation).filter(CrmConversation.id == conv_id).first()
    if not conv:
        return {"error": "Conversa não encontrada"}

    # Zera unread
    conv.unread = 0
    db.commit()

    msgs = db.query(CrmMessage)\
        .filter(CrmMessage.conversation_id == conv_id)\
        .order_by(CrmMessage.sent_at.asc()).all()

    return {
        "conversation": {
            "id": conv.id,
            "phone": conv.phone,
            "contact_name": conv.contact_name,
            "contact_email": conv.contact_email,
            "stage": conv.stage,
            "ai_active": conv.ai_active,
            "attendant": conv.attendant,
            "sector": conv.sector,
            "notes": conv.notes,
        },
        "messages": [
            {
                "id": m.id,
                "direction": m.direction,
                "content": m.content,
                "sent_by": m.sent_by,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
            }
            for m in msgs
        ],
    }


# ─── API: enviar mensagem ──────────────────────────────────────────────────────

@router.post("/crm/conversations/{conv_id}/send")
async def send_message(conv_id: int, request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    content = data.get("content", "").strip()
    attendant = data.get("attendant", "Atendente")

    if not content:
        return {"error": "Mensagem vazia"}

    conv = db.query(CrmConversation).filter(CrmConversation.id == conv_id).first()
    if not conv:
        return {"error": "Conversa não encontrada"}

    try:
        send_whatsapp_message(conv.phone, content, db)
    except Exception as e:
        return {"error": f"Falha ao enviar: {e}"}

    db.add(CrmMessage(
        conversation_id=conv_id,
        direction="out",
        content=content,
        sent_by=attendant,
    ))
    conv.updated_at = datetime.utcnow()
    db.commit()

    return {"status": "sent"}


# ─── API: atualizar conversa ───────────────────────────────────────────────────

@router.patch("/crm/conversations/{conv_id}")
async def update_conversation(conv_id: int, request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    conv = db.query(CrmConversation).filter(CrmConversation.id == conv_id).first()
    if not conv:
        return {"error": "Conversa não encontrada"}

    if "stage" in data:
        conv.stage = data["stage"]
    if "ai_active" in data:
        conv.ai_active = data["ai_active"]
    if "attendant" in data:
        conv.attendant = data["attendant"]
    if "sector" in data:
        conv.sector = data["sector"]
    if "notes" in data:
        conv.notes = data["notes"]
    if "contact_name" in data:
        conv.contact_name = data["contact_name"]
    if "contact_email" in data:
        conv.contact_email = data["contact_email"]

    db.commit()
    return {"status": "updated"}


# ─── API: deletar conversa ─────────────────────────────────────────────────────

@router.delete("/crm/conversations/{conv_id}")
def delete_conversation(conv_id: int, db: Session = Depends(get_db)):
    conv = db.query(CrmConversation).filter(CrmConversation.id == conv_id).first()
    if not conv:
        return {"error": "Conversa não encontrada"}
    db.query(CrmMessage).filter(CrmMessage.conversation_id == conv_id).delete()
    db.delete(conv)
    db.commit()
    return {"status": "deleted"}


# ─── API: enfileirar lead na recuperação manualmente ──────────────────────────

# ─── API: ticket de suporte do app desktop ───────────────────────────────────

@router.post("/suporte-ticket")
async def suporte_ticket(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
    except Exception:
        return {"error": "JSON inválido"}

    email     = (data.get("email") or "").strip()
    nome      = (data.get("nome") or "Cliente").strip()
    mensagem  = (data.get("mensagem") or "").strip()
    device_id = (data.get("device_id") or "").strip()

    if not mensagem:
        return {"error": "Mensagem não pode estar vazia"}

    # Número da Maia
    MAIA_NUMBER = "5545999539960"

    # Monta mensagem para a Maia
    texto_maia = (
        f"🛡️ *SUPORTE — Guardian Shield*\n\n"
        f"👤 *Cliente:* {nome}\n"
        f"📧 *E-mail:* {email or 'não informado'}\n"
        f"📱 *Dispositivo:* {device_id or 'não informado'}\n\n"
        f"💬 *Mensagem:*\n{mensagem}"
    )

    # Envia WhatsApp para a Maia
    try:
        send_whatsapp_message(MAIA_NUMBER, texto_maia, db)
    except Exception as e:
        logger.error(f"[SUPORTE] erro ao enviar WA para Maia: {e}")
        return {"error": "Falha ao enviar mensagem. Tente novamente."}

    # Cria ou atualiza conversa no CRM com stage 'support'
    try:
        conv = db.query(CrmConversation).filter(
            CrmConversation.contact_email == email
        ).first() if email else None

        if conv:
            conv.stage = "support"
            conv.updated_at = datetime.utcnow()
        else:
            conv = CrmConversation(
                phone=email or device_id or "app-suporte",
                contact_name=nome,
                contact_email=email,
                stage="support",
                ai_enabled=False,
            )
            db.add(conv)
            db.flush()

        # Registra a mensagem no histórico do CRM
        msg = CrmMessage(
            conversation_id=conv.id,
            direction="in",
            content=mensagem,
            timestamp=datetime.utcnow(),
        )
        db.add(msg)
        db.commit()
        logger.info(f"[SUPORTE] ticket criado conv_id={conv.id} email={email}")
    except Exception as e:
        logger.error(f"[SUPORTE] erro ao salvar CRM: {e}")
        # Não retorna erro — a mensagem já foi enviada para a Maia

    return {"ok": True}


# ─── API: enfileirar lead na recuperação manualmente ──────────────────────────

@router.post("/crm/conversations/{conv_id}/recovery-enqueue")
async def crm_recovery_enqueue(conv_id: int, db: Session = Depends(get_db)):
    conv = db.query(CrmConversation).filter(CrmConversation.id == conv_id).first()
    if not conv:
        return {"error": "Conversa não encontrada"}

    phone = conv.phone.replace("+", "").replace(" ", "").replace("-", "")
    try:
        from services.recovery_service import criar_fila_abandono
        criar_fila_abandono(phone=phone, email=conv.contact_email or "", nome=conv.contact_name or "", db=db)
        return {"ok": True}
    except Exception as e:
        logger.error(f"[CRM] recovery-enqueue erro: {e}")
        return {"error": str(e)}
