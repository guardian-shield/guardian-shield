"""Follow-up automático de leads que não responderam."""
import asyncio
import logging
import time
import urllib.request
import json
import os
from datetime import datetime, timedelta

from database import SessionLocal
from models import CrmConversation, CrmMessage

logger = logging.getLogger("guardian")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Intervalos de follow-up (a partir da última mensagem do usuário)
FOLLOWUP_INTERVALS = [
    timedelta(minutes=30),  # 1º follow-up
    timedelta(hours=2),     # 2º follow-up
    timedelta(hours=4),     # 3º follow-up
]
DAILY_INTERVAL = timedelta(hours=24)

QUIET_START = 22  # 22h
QUIET_END = 7     # 7h


def _is_quiet_hours() -> bool:
    hour = datetime.now().hour
    return hour >= QUIET_START or hour < QUIET_END


def _get_followup_message(history: list, followup_index: int) -> str:
    """Gera mensagem de follow-up contextual usando Claude."""
    if not ANTHROPIC_API_KEY:
        return ""

    if followup_index == 0:
        instrucao = "O lead parou de responder há 30 minutos. Mande uma mensagem curta e natural retomando a conversa, sem pressão. Baseie no que foi conversado."
    elif followup_index == 1:
        instrucao = "O lead não respondeu há 2 horas. Mande uma mensagem curta criando curiosidade ou oferecendo ajuda, ainda sem pressão."
    elif followup_index == 2:
        instrucao = "O lead não respondeu há 4 horas. Pode ser mais direto, perguntar se ainda tem interesse ou se surgiu alguma dúvida."
    else:
        instrucao = "Follow-up diário. Mande uma mensagem de 1-2 linhas variando o ângulo — pode ser um dado novo, uma pergunta, ou algo que gere curiosidade. Não repita mensagens anteriores."

    messages = []
    for msg in history[-8:]:
        role = "user" if msg["direction"] == "in" else "assistant"
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + msg["content"]
        else:
            messages.append({"role": role, "content": msg["content"]})

    # Garante que termina com mensagem do usuário para que o Claude responda
    messages.append({"role": "user", "content": f"[INSTRUÇÃO INTERNA — NÃO MENCIONAR]: {instrucao}"})

    system = (
        "Você é a Maia, atendente do Guardian Shield. "
        "Escreva APENAS a mensagem de follow-up, sem explicação, sem prefácio. "
        "Seja natural, curta (máximo 2-3 linhas), como se fosse uma pessoa real no WhatsApp. "
        "Não use saudações como 'Olá' se já conversou antes. Não mencione que é IA."
    )

    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "system": system,
        "messages": messages,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"[FOLLOWUP AI] Erro: {e}")
        return ""


async def process_followups():
    """Verifica e envia follow-ups pendentes."""
    from services.whatsapp_service import send_whatsapp_message

    db = SessionLocal()
    try:
        now = datetime.utcnow()

        # Só conversa com IA ativa e stage != active (não tem licença)
        convs = db.query(CrmConversation).filter(
            CrmConversation.ai_active == True,
            CrmConversation.stage.notin_(["active", "cancelled"]),
        ).all()

        for conv in convs:
            # Última mensagem da conversa
            last_msg = db.query(CrmMessage)\
                .filter(CrmMessage.conversation_id == conv.id)\
                .order_by(CrmMessage.sent_at.desc()).first()

            if not last_msg or not last_msg.sent_at:
                continue

            # Só faz follow-up se última mensagem foi do usuário (ele não respondeu ao AI)
            if last_msg.direction != "in":
                continue

            last_user_time = last_msg.sent_at
            count = conv.followup_count or 0

            # Calcula se deve enviar
            should_send = False
            if count < len(FOLLOWUP_INTERVALS):
                threshold = FOLLOWUP_INTERVALS[count]
                should_send = (now - last_user_time) >= threshold
            else:
                # Follow-up diário a partir do último enviado
                ref_time = conv.last_followup_at or last_user_time
                should_send = (now - ref_time) >= DAILY_INTERVAL

            if not should_send:
                continue

            # Respeita horário de silêncio
            if _is_quiet_hours():
                continue

            # Busca histórico
            history = db.query(CrmMessage)\
                .filter(CrmMessage.conversation_id == conv.id)\
                .order_by(CrmMessage.sent_at.desc())\
                .limit(10).all()
            history = [{"direction": m.direction, "content": m.content} for m in reversed(history)]

            # Gera mensagem
            followup_text = _get_followup_message(history, count)
            if not followup_text:
                # API falhou — incrementa contador p/ não ficar tentando indefinidamente
                conv.followup_count = count + 1
                conv.last_followup_at = now
                db.commit()
                logger.warning(f"[FOLLOWUP] API retornou vazio para {conv.phone} — contador incrementado sem envio")
                continue

            # Delay humanizado
            await asyncio.sleep(3)

            try:
                send_whatsapp_message(conv.phone, followup_text, db)
                db.add(CrmMessage(
                    conversation_id=conv.id,
                    direction="out",
                    content=followup_text,
                    sent_by="ai_followup",
                ))
                conv.followup_count = count + 1
                conv.last_followup_at = now
                conv.updated_at = now
                db.commit()
                logger.warning(f"[FOLLOWUP] #{count + 1} enviado para {conv.phone}: {followup_text[:60]}")
            except Exception as e:
                logger.error(f"[FOLLOWUP] Erro ao enviar para {conv.phone}: {e}")

    except Exception as e:
        logger.error(f"[FOLLOWUP] Erro geral: {e}")
    finally:
        db.close()


async def process_license_recovery():
    """Verifica licenças expirando (7 dias) ou expiradas e inicia recuperação no CRM."""
    from services.whatsapp_service import send_whatsapp_message
    from models import User

    if _is_quiet_hours():
        return

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        # Licenças que expiram nos próximos 7 dias ou já expiraram há até 30 dias
        soon = now + timedelta(days=7)
        cutoff = now - timedelta(days=30)

        users = db.query(User).filter(
            User.expires_at != None,
            User.expires_at >= cutoff,
            User.whatsapp != None,
        ).all()

        for user in users:
            if not user.whatsapp or not user.expires_at:
                continue

            phone = user.whatsapp.replace("+", "").replace(" ", "").replace("-", "")
            days_left = (user.expires_at - now).days
            is_expiring = 0 < days_left <= 7
            is_expired = days_left <= 0

            if not is_expiring and not is_expired:
                continue

            # Busca ou cria conversa no CRM
            conv = db.query(CrmConversation).filter(CrmConversation.phone == phone).first()
            if not conv:
                conv = CrmConversation(
                    phone=phone,
                    contact_email=user.email,
                    stage="expiring",
                    ai_active=True,
                )
                db.add(conv)
                db.commit()
                db.refresh(conv)
            else:
                if conv.stage == "active":
                    conv.stage = "expiring"
                    db.commit()

            # Verifica se já mandou mensagem de recuperação hoje
            last_msg = db.query(CrmMessage).filter(
                CrmMessage.conversation_id == conv.id,
                CrmMessage.sent_by == "ai_recovery",
            ).order_by(CrmMessage.sent_at.desc()).first()

            if last_msg and last_msg.sent_at:
                hours_since = (now - last_msg.sent_at).total_seconds() / 3600
                if hours_since < 23:
                    continue  # já mandou hoje

            # Gera mensagem de recuperação contextual
            if is_expiring:
                msg = _get_recovery_message(user.email, days_left, expired=False)
            else:
                msg = _get_recovery_message(user.email, abs(days_left), expired=True)

            if not msg:
                continue

            await asyncio.sleep(3)
            try:
                send_whatsapp_message(phone, msg, db)
                db.add(CrmMessage(
                    conversation_id=conv.id,
                    direction="out",
                    content=msg,
                    sent_by="ai_recovery",
                ))
                db.commit()
                logger.warning(f"[RECOVERY] Enviado para {phone} — {'expirando' if is_expiring else 'expirado'} {days_left}d")
            except Exception as e:
                logger.error(f"[RECOVERY] Erro ao enviar para {phone}: {e}")

    except Exception as e:
        logger.error(f"[RECOVERY] Erro geral: {e}")
    finally:
        db.close()


def _get_recovery_message(email: str, days: int, expired: bool) -> str:
    """Gera mensagem de recuperação via Claude."""
    if not ANTHROPIC_API_KEY:
        return ""

    if expired:
        context = f"A licença do Guardian Shield deste cliente expirou há {days} dias. Mande uma mensagem curta e direta para reativar, mencionando que o plano anual é R$499 e o link: https://guardian.grupomayconsantos.com.br/pagar"
    else:
        context = f"A licença do Guardian Shield deste cliente expira em {days} dias. Avise de forma natural e ofereça a renovação, mencionando o link: https://guardian.grupomayconsantos.com.br/pagar"

    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 200,
        "system": "Você é a Maia, atendente do Guardian Shield. Escreva APENAS a mensagem de WhatsApp, sem prefácio. Curta, natural, máximo 3 linhas.",
        "messages": [{"role": "user", "content": context}],
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"[RECOVERY AI] {e}")
        return ""


async def run_followup_loop():
    """Loop em background — verifica a cada 60 segundos."""
    logger.warning("[FOLLOWUP] Scheduler iniciado.")
    while True:
        try:
            await asyncio.sleep(60)
            await process_followups()
            await process_license_recovery()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[FOLLOWUP] Loop error: {e}")
