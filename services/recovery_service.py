"""
Serviço de recuperação de leads — 3 fluxos:
  1. abandonment — lead gerou PIX ou tentou cartão e não pagou
  2. renewal     — licença expirou ou está expirando e não renovou

Cada fluxo tem uma sequência de steps com intervalos.
A Maia gera as mensagens contextualizadas via Claude.
Se o lead responder qualquer coisa → pausa a fila e IA assume.
Se o lead disser que não quer → cancela a fila.
Se o lead pagar → cancela e muda para suporte.
"""
import asyncio
import logging
import urllib.request
import json
import os
from datetime import datetime, timedelta

from database import SessionLocal
from models import RecoveryQueue, CrmConversation, CrmMessage

logger = logging.getLogger("guardian")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LINK_PAGAMENTO = "https://guardian.grupomayconsantos.com.br/pagar"

# ── Intervalos por fluxo e step ───────────────────────────────────────────────

ABANDONMENT_STEPS = [
    timedelta(minutes=5),    # step 0 — primeiro contato (já existe, mas agora é parte da fila)
    timedelta(hours=1),      # step 1 — "ainda dá tempo, qual foi o receio?"
    timedelta(hours=3),      # step 2 — prova social / ângulo diferente
    timedelta(hours=23),     # step 3 — urgência / última tentativa (dia seguinte 9h aprox)
]

RENEWAL_STEPS = [
    timedelta(days=7),       # step 0 — 7 dias antes do vencimento
    timedelta(days=4),       # step 1 — 3 dias antes (delta do anterior)
    timedelta(days=3),       # step 2 — dia do vencimento
    timedelta(days=2),       # step 3 — 2 dias após expirar
    timedelta(days=5),       # step 4 — 7 dias após expirar
]

# Nurturing de trial grátis — conversão para anual nos últimos 2 dias
TRIAL_NURTURE_STEPS = [
    timedelta(hours=2),      # step 0 — dia 0 · dica de primeiros passos
    timedelta(days=3),       # step 1 — dia 3 · check-in (como está indo?)
    timedelta(days=2),       # step 2 — dia 5 · início da conversão (suave)
    timedelta(days=1),       # step 3 — dia 6 · pergunta sobre uso + urgência leve
    timedelta(days=1),       # step 4 — dia 7 · pergunta sobre uso + último dia
]

# Ativação de trial — quem cadastrou mas ainda não baixou/ativou (1x/dia até ativar ou expirar)
TRIAL_ACTIVATION_STEPS = [timedelta(days=1)] * 7  # máximo 7 tentativas diárias

# ── Prompts contextuais por fluxo/step ───────────────────────────────────────

def _system_abandonment(step: int) -> str:
    base = (
        "Você é a Maia, atendente do Guardian Shield — software de blindagem digital para assistências técnicas. "
        "Está tentando recuperar um lead que iniciou um pagamento mas não concluiu. "
        "Escreva APENAS a mensagem de WhatsApp, sem prefácio, sem explicação. "
        "Curta (máximo 3 linhas), natural, como uma pessoa real no WhatsApp. "
        "Não use saudações formais. Nunca mencione que é IA. "
        "Sempre inclua o link ao final quando relevante: " + LINK_PAGAMENTO
    )
    instrucoes = [
        "Primeiro contato — o lead gerou o PIX ou tentou pagar mas não finalizou há 5 minutos. "
        "Seja curiosa e leve, pergunte se ficou alguma dúvida ou se travou em alguma etapa. Sem pressão.",

        "O lead não respondeu há 1 hora. Agora pode ser um pouco mais direto. "
        "Diga que ainda dá tempo de garantir, pergunte qual foi o receio ou o que impediu. "
        "Tom: parceiro, não vendedor.",

        "O lead não respondeu há 3 horas. Mude o ângulo — use prova social ou um dado concreto. "
        "Ex: 'Outros técnicos faturaram R$100 na primeira blindagem desta semana...' "
        "Finalize com uma pergunta que force resposta sim/não.",

        "Última tentativa — dia seguinte. Use urgência real (vagas limitadas ou preço promocional). "
        "Tom direto mas sem desespero. Se não tiver interesse, tudo bem — só queria saber.",
    ]
    instrucao = instrucoes[min(step, len(instrucoes)-1)]
    return base + "\n\nINSTRUÇÃO: " + instrucao


def _system_renewal(step: int, days_left: int, expired: bool) -> str:
    base = (
        "Você é a Maia, atendente do Guardian Shield. "
        "Este cliente já usa ou usou o produto — é uma conversa de renovação/fidelização, não de venda fria. "
        "Escreva APENAS a mensagem de WhatsApp, sem prefácio. "
        "Curta (máximo 3 linhas), tom de quem conhece o cliente. "
        "Sempre inclua o link ao final: " + LINK_PAGAMENTO
    )
    if not expired:
        instrucoes = [
            f"A licença vence em {days_left} dias. Avise de forma natural, sem alarme. Mostre o valor do plano anual (R$299).",
            f"Faltam {days_left} dias. Um pouco mais de urgência — pergunte se vai renovar, facilite o processo.",
            "Hoje é o último dia de acesso. Tom de cuidado, não de cobrança. Dê o link direto.",
        ]
    else:
        instrucoes = [
            f"A licença expirou há {abs(days_left)} dias. Pergunte se sentiu falta, se ainda tem interesse. Tom leve.",
            f"Já faz {abs(days_left)} dias sem acesso. Última tentativa — mostre o que está perdendo. Tom direto mas respeitoso.",
        ]
    instrucao = instrucoes[min(step % len(instrucoes), len(instrucoes)-1)]
    return base + "\n\nINSTRUÇÃO: " + instrucao


def _system_trial_nurture(step: int) -> str:
    """Mensagens para quem está no trial grátis de 7 dias — foco em uso e conversão nos últimos 2 dias."""
    LINK_ANUAL = "https://guardian.grupomayconsantos.com.br/pagar"
    base = (
        "Você é a Maia, atendente do Guardian Shield. "
        "Este usuário está no período de teste gratuito de 7 dias. "
        "Escreva APENAS a mensagem de WhatsApp, sem prefácio, máximo 3-4 linhas. "
        "Tom natural, de quem conhece a pessoa. Nunca mencione que é IA."
    )
    instrucoes = [
        # step 0 — 2h após cadastro — primeiros passos
        "O usuário acabou de se cadastrar há 2 horas. Envie uma dica prática e curta para conectar o primeiro celular. "
        "Mostre que é simples: cabo USB, ativar Depuração USB, clicar Conectar no app. Finalize convidando a testar hoje mesmo. "
        "Tom animado e acolhedor.",

        # step 1 — dia 3 — check-in
        "Já se passaram 3 dias do teste. Pergunte se já conseguiu conectar algum celular e rodar a primeira blindagem. "
        "Se a pessoa ainda não tentou, ofereça ajuda rápida. Não mencione preço ainda — foco em uso e resultado.",

        # step 2 — dia 5 — início suave da conversão (2 dias antes do fim)
        "Faltam 2 dias para o teste acabar. Mencione isso de forma leve e natural. "
        "Mostre o valor do plano anual (R$299/ano) e faça a conta rápida: "
        "5 blindagens de R$100 = R$500 numa semana. A licença se paga sozinha. "
        "Não pressione — só plante a semente. "
        "Link do pagamento: " + LINK_ANUAL,

        # step 3 — dia 6 — pergunta sobre uso + urgência leve
        "É o 6º dia do teste. Pergunte como está sendo a experiência — se conseguiu conectar algum celular, "
        "se rodou alguma blindagem, o que achou do processo. Mostre interesse genuíno. "
        "No final, mencione de forma leve que o teste acaba amanhã e dê o link caso queira garantir o anual. "
        "Tom: curiosa e parceira, não de vendedora. "
        "Link: " + LINK_ANUAL,

        # step 4 — dia 7 — pergunta sobre uso + último dia
        "Hoje é o último dia do teste. Comece perguntando: o que achou no geral? Conseguiu usar? "
        "Teve alguma dificuldade ou correu tudo bem? Mostre que você quer saber a opinião real. "
        "Depois mencione que o acesso encerra hoje e dê o link direto para quem quiser continuar. "
        "Tom: humana, sem pressão excessiva — quem quer, clica. "
        "Link: " + LINK_ANUAL,
    ]
    instrucao = instrucoes[min(step, len(instrucoes) - 1)]
    return base + "\n\nINSTRUÇÃO: " + instrucao


def _system_trial_activation(step: int) -> str:
    """Mensagens para quem se cadastrou no trial mas ainda não baixou/ativou o software."""
    LINK_DOWNLOAD = "https://guardian.grupomayconsantos.com.br/download"
    LINK_TUTORIAL = "https://www.youtube.com/watch?v=92dTghZ8RQc"
    base = (
        "Você é a Maia, atendente do Guardian Shield. "
        "Este usuário fez o cadastro do teste grátis de 7 dias mas ainda não baixou ou ativou o software. "
        "Escreva APENAS a mensagem de WhatsApp, sem prefácio, máximo 3 linhas. "
        "Tom: prestativo, sem pressão. Nunca mencione que é IA."
    )
    instrucoes = [
        # step 0 — 1 dia após cadastro
        "O usuário se cadastrou ontem mas ainda não baixou o Guardian Shield. "
        "Pergunte se teve alguma dificuldade para baixar ou instalar. "
        "Dê o link de download e o link do tutorial em vídeo (que mostra o passo a passo completo). "
        "Download: " + LINK_DOWNLOAD + " | Tutorial: " + LINK_TUTORIAL,

        # step 1 — dia 2
        "Já faz 2 dias que o usuário se cadastrou e ainda não ativou. "
        "Pergunte se está com dúvida na instalação — muita gente trava no cabo USB ou no modo desenvolvedor. "
        "Ofereça ajuda, dê o link de download e mencione que tem um tutorial em vídeo mostrando tudo. "
        "Download: " + LINK_DOWNLOAD + " | Tutorial: " + LINK_TUTORIAL,

        # step 2 — dia 3
        "Terceiro dia sem ativar. Seja direta mas simpática — "
        "diga que o período de 7 dias já começou a correr e que seria uma pena não aproveitar. "
        "Pergunte o que está impedindo: é dificuldade técnica, falta de tempo ou outra coisa? "
        "Link: " + LINK_DOWNLOAD,

        # step 3 — dia 4
        "O usuário ainda não ativou e já passou metade do teste. "
        "Tom gentil mas com alguma urgência: já passaram 4 dos 7 dias. "
        "Diga que se precisar de ajuda para instalar você acompanha passo a passo. "
        "Link: " + LINK_DOWNLOAD,

        # step 4 — dia 5
        "5 dias de teste e ainda sem ativar. Pergunte se a pessoa ainda tem interesse "
        "ou se algo mudou — sem julgamento, só para entender. "
        "Reforce que faltam só 2 dias e que o download é rápido. "
        "Link: " + LINK_DOWNLOAD,

        # step 5 — dia 6
        "Penúltimo dia do teste sem nenhuma ativação. "
        "Mensagem curta e direta: amanhã o período acaba. "
        "Pergunte se quer ajuda para configurar agora, porque ainda dá tempo. "
        "Link: " + LINK_DOWNLOAD,

        # step 6 — dia 7
        "Último dia. Último aviso sobre o download. "
        "Tom empático: sem pressão, mas deixa claro que o acesso encerra hoje. "
        "Se não for o momento, tudo bem — só deixa o link para quando precisar. "
        "Link: " + LINK_DOWNLOAD,
    ]
    instrucao = instrucoes[min(step, len(instrucoes) - 1)]
    return base + "\n\nINSTRUÇÃO: " + instrucao


def _system_trial_expired(step: int) -> str:
    """Mensagens diárias para quem teve trial expirado e não converteu — sem fim definido."""
    LINK_ANUAL  = "https://guardian.grupomayconsantos.com.br/pagar?plano=anual"
    LINK_MENSAL = "https://guardian.grupomayconsantos.com.br/pagar"
    base = (
        "Você é a Maia, atendente do Guardian Shield. "
        "Este usuário fez o teste grátis de 7 dias mas o período expirou e ele não contratou. "
        "Escreva APENAS a mensagem de WhatsApp, sem prefácio, máximo 3-4 linhas. "
        "Tom: natural, sem pressão excessiva. Varie o ângulo a cada mensagem. "
        "Nunca repita exatamente a mesma abordagem. Nunca mencione que é IA. "
        "Sempre inclua o link ao final."
    )
    # Ciclo de 7 ângulos diferentes que se repetem
    angulos = [
        # 0 — dia após expirar
        "O teste acabou ontem. Pergunte o que achou da experiência — gostou? Teve alguma dificuldade? "
        "Diga que ficou a disposição e que quem quiser continuar garante o anual por R$299. "
        "Link: " + LINK_ANUAL,

        # 1 — prova social
        "Use um dado concreto de resultado: 'Técnicos que usam o Guardian Shield cobram em média R$100 por blindagem — "
        "5 blindagens numa semana já pagam a licença anual.' "
        "Pergunte se a pessoa já pensou em oferecer esse serviço. Link: " + LINK_ANUAL,

        # 2 — remover dor
        "Aborde a dor: cliente que volta com vírus 2 dias depois e culpa o técnico. "
        "Com o Guardian Shield o problema não volta — e tem certificado de garantia para provar. "
        "Pergunte se isso já aconteceu com ela. Link: " + LINK_ANUAL,

        # 3 — ancoragem de preço
        "Faça a conta: se cobrar R$80 por blindagem, em 4 atendimentos a licença anual se paga. "
        "O resto é lucro. Não precisa de peça, não precisa de mão extra. "
        "Ainda dá para garantir — link: " + LINK_ANUAL,

        # 4 — oferta mensal como porta de entrada
        "Se o anual parecer um compromisso grande, diga que também tem o plano mensal para testar sem tanto risco. "
        "Assim ela pode começar a cobrar dos clientes, sentir o retorno, e depois decide se renova. "
        "Link: " + LINK_MENSAL,

        # 5 — depoimento / resultado real
        "Mostre que funciona na prática: 'O Maycon, criador do Guardian Shield, gerou R$35 mil líquidos "
        "com esse serviço em 8 meses na própria assistência dele.' "
        "Pergunte o que impede a pessoa de começar. Link: " + LINK_ANUAL,

        # 6 — check-in simples
        "Mensagem curta e leve: 'Oi, tudo bem? Só passando para ver se surgiu alguma dúvida ou se posso ajudar em algo. "
        "Se quiser reativar é só clicar aqui →' "
        "Link: " + LINK_ANUAL,
    ]
    instrucao = angulos[step % len(angulos)]  # rotaciona ciclicamente
    return base + "\n\nINSTRUÇÃO: " + instrucao


def _system_support_onboarding(step: int) -> str:
    """Mensagens de onboarding para quem acabou de pagar."""
    base = (
        "Você é a Maia, atendente do Guardian Shield. "
        "Este cliente acabou de comprar — agora seu papel é suporte, não venda. "
        "Tom: acolhedor, técnico quando necessário, celebra as conquistas. "
        "Escreva APENAS a mensagem de WhatsApp, sem prefácio, máximo 3 linhas."
    )
    instrucoes = [
        "Dia 3 após a compra. Pergunte se já conseguiu conectar o primeiro celular. "
        "Se não, ofereça ajuda rápida. Tom descontraído.",

        "Dia 7. Pergunte como tá indo — já fez alguma blindagem? "
        "Mostre interesse genuíno no resultado do cliente.",

        "Dia 15. Comemore — 2 semanas de Guardian Shield. "
        "Pergunte se já recuperou o investimento. Reforce o valor.",
    ]
    instrucao = instrucoes[min(step, len(instrucoes)-1)]
    return base + "\n\nINSTRUÇÃO: " + instrucao


# ── Geração de mensagem via Claude ───────────────────────────────────────────

def _gerar_mensagem(system: str, history: list, nome: str) -> str:
    if not ANTHROPIC_API_KEY:
        return ""

    messages = []
    for msg in history[-8:]:
        role = "user" if msg["direction"] == "in" else "assistant"
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + msg["content"]
        else:
            messages.append({"role": role, "content": msg["content"]})

    # Instrução final com nome do lead para personalização
    context = f"[O nome do lead é: {nome or 'não informado'}. Use se fizer sentido natural.]"
    messages.append({"role": "user", "content": context})

    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 350,
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
            return data["content"][0]["text"].strip()
    except Exception as e:
        logger.error(f"[RECOVERY AI] Erro: {e}")
        return ""


# ── Detecção de intenção de desistência ──────────────────────────────────────

CANCELAMENTO_KEYWORDS = [
    "não quero", "nao quero", "não tenho interesse", "nao tenho interesse",
    "para de me mandar", "para de mandar", "me tira", "tira meu numero",
    "não preciso", "nao preciso", "desisti", "não vou comprar", "nao vou comprar",
    "não quero mais", "nao quero mais", "cancela", "remover", "sair da lista",
    "chega", "para", "stop",
]

def _quer_cancelar(texto: str) -> bool:
    t = texto.lower().strip()
    return any(k in t for k in CANCELAMENTO_KEYWORDS)


# ── Criação de entrada na fila ────────────────────────────────────────────────

def criar_fila_abandono(phone: str, email: str = "", nome: str = "", db=None):
    """Chamado quando lead gera PIX ou tenta cartão mas não paga."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        # Evita duplicata — cancela fila anterior do mesmo telefone se ainda pendente
        existente = db.query(RecoveryQueue).filter(
            RecoveryQueue.phone == phone,
            RecoveryQueue.tipo == "abandonment",
            RecoveryQueue.status == "pending",
        ).first()
        if existente:
            existente.step = 0
            existente.next_send_at = datetime.utcnow() + ABANDONMENT_STEPS[0]
            existente.status = "pending"
            existente.updated_at = datetime.utcnow()
            db.commit()
            return

        db.add(RecoveryQueue(
            phone=phone,
            email=email,
            nome=nome,
            tipo="abandonment",
            step=0,
            next_send_at=datetime.utcnow() + ABANDONMENT_STEPS[0],
            status="pending",
        ))
        db.commit()
        logger.warning(f"[RECOVERY] Fila abandono criada para {phone}")
    finally:
        if close_db:
            db.close()


def criar_fila_renovacao(phone: str, email: str = "", nome: str = "", dias_para_vencer: int = 7, db=None):
    """Chamado pelo scheduler quando detecta licença expirando/expirada."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        existente = db.query(RecoveryQueue).filter(
            RecoveryQueue.phone == phone,
            RecoveryQueue.tipo == "renewal",
            RecoveryQueue.status == "pending",
        ).first()
        if existente:
            return  # já tem fila ativa

        db.add(RecoveryQueue(
            phone=phone,
            email=email,
            nome=nome,
            tipo="renewal",
            step=0,
            next_send_at=datetime.utcnow() + timedelta(hours=1),  # primeiro disparo em 1h
            status="pending",
        ))
        db.commit()
        logger.warning(f"[RECOVERY] Fila renovação criada para {phone}")
    finally:
        if close_db:
            db.close()


def cancelar_fila(phone: str, tipo: str = None, db=None):
    """Cancela fila quando lead paga ou diz que não quer."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        q = db.query(RecoveryQueue).filter(
            RecoveryQueue.phone == phone,
            RecoveryQueue.status.in_(["pending", "paused"]),
        )
        if tipo:
            q = q.filter(RecoveryQueue.tipo == tipo)
        for item in q.all():
            item.status = "cancelled"
            item.updated_at = datetime.utcnow()
        db.commit()
    finally:
        if close_db:
            db.close()


def pausar_fila(phone: str, db=None):
    """Pausa fila quando lead responde — IA assume a conversa."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        for item in db.query(RecoveryQueue).filter(
            RecoveryQueue.phone == phone,
            RecoveryQueue.status == "pending",
        ).all():
            item.status = "paused"
            item.updated_at = datetime.utcnow()
        db.commit()
    finally:
        if close_db:
            db.close()


# ── Processamento da fila ─────────────────────────────────────────────────────

async def process_recovery_queue():
    from services.whatsapp_service import send_whatsapp_message

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        # Busca itens pendentes cujo tempo já chegou
        itens = db.query(RecoveryQueue).filter(
            RecoveryQueue.status == "pending",
            RecoveryQueue.next_send_at <= now,
        ).all()

        for item in itens:
            # Verifica horário silencioso (22h-7h)
            hora_local = datetime.now().hour
            if hora_local >= 22 or hora_local < 7:
                continue

            # Busca conversa no CRM
            conv = db.query(CrmConversation).filter(
                CrmConversation.phone == item.phone
            ).first()

            # Se lead já está ativo (pagou) → cancela
            if conv and conv.stage == "active":
                item.status = "completed"
                item.updated_at = now
                db.commit()
                continue

            # Busca histórico para contexto
            history = []
            if conv:
                msgs = db.query(CrmMessage)\
                    .filter(CrmMessage.conversation_id == conv.id)\
                    .order_by(CrmMessage.sent_at.desc())\
                    .limit(10).all()
                history = [{"direction": m.direction, "content": m.content} for m in reversed(msgs)]

                # Se última mensagem foi do lead → pausa (ele respondeu)
                last_msg = db.query(CrmMessage)\
                    .filter(CrmMessage.conversation_id == conv.id)\
                    .order_by(CrmMessage.sent_at.desc()).first()
                if last_msg and last_msg.direction == "in":
                    # Verifica se quer cancelar
                    if _quer_cancelar(last_msg.content):
                        item.status = "cancelled"
                        if conv:
                            conv.stage = "cancelled"
                            conv.ai_active = False
                        db.commit()
                        logger.warning(f"[RECOVERY] {item.phone} cancelou — removido da fila")
                    else:
                        item.status = "paused"
                        db.commit()
                        logger.warning(f"[RECOVERY] {item.phone} respondeu — fila pausada, IA assume")
                    continue

            # Gera system prompt conforme tipo e step
            if item.tipo == "abandonment":
                system = _system_abandonment(item.step)
            elif item.tipo == "renewal":
                # Calcula dias restantes para contexto
                from models import User
                user = db.query(User).filter(User.email == item.email).first() if item.email else None
                days_left = 0
                expired = True
                if user and user.expires_at:
                    diff = (user.expires_at - now).days
                    days_left = diff
                    expired = diff <= 0
                system = _system_renewal(item.step, days_left, expired)
            elif item.tipo == "support":
                system = _system_support_onboarding(item.step)
            elif item.tipo == "trial_nurture":
                # Se o trial já foi convertido para plano pago, cancela nurturing
                from models import User
                user_trial = db.query(User).filter(User.email == item.email).first() if item.email else None
                if user_trial and user_trial.plan_type in ("anual", "anual79", "anual199", "mensal"):
                    item.status = "completed"
                    db.commit()
                    continue
                system = _system_trial_nurture(item.step)

            elif item.tipo == "trial_activation":
                # Cancela se o usuário ativou (tem hwid) ou se o trial expirou
                from models import User
                user_act = db.query(User).filter(User.email == item.email).first() if item.email else None
                if user_act:
                    if user_act.hwid_1:
                        # Usuário ativou o software — não precisa mais desse fluxo
                        item.status = "completed"
                        db.commit()
                        logger.warning(f"[RECOVERY] trial_activation cancelado — {item.phone} ativou")
                        continue
                    if user_act.plan_type in ("anual", "anual79", "anual199", "mensal"):
                        item.status = "completed"
                        db.commit()
                        continue
                system = _system_trial_activation(item.step)

            elif item.tipo == "trial_expired":
                # Cancela se o usuário converteu para plano pago
                from models import User
                user_exp = db.query(User).filter(User.email == item.email).first() if item.email else None
                if user_exp and user_exp.plan_type in ("anual", "anual79", "anual199", "mensal"):
                    item.status = "completed"
                    db.commit()
                    logger.warning(f"[RECOVERY] trial_expired cancelado — {item.phone} converteu")
                    continue
                system = _system_trial_expired(item.step)

            else:
                item.status = "cancelled"
                db.commit()
                continue

            # Gera mensagem via Claude
            texto = _gerar_mensagem(system, history, item.nome or "")
            if not texto:
                # API falhou — tenta novamente em 30min
                item.next_send_at = now + timedelta(minutes=30)
                db.commit()
                logger.warning(f"[RECOVERY] API falhou para {item.phone} — retry em 30min")
                continue

            # Envia
            try:
                send_whatsapp_message(item.phone, texto, db)

                # Salva no CRM
                if not conv:
                    conv = CrmConversation(
                        phone=item.phone,
                        contact_name=item.nome or item.phone,
                        contact_email=item.email,
                        stage="initiated",
                        ai_active=True,
                    )
                    db.add(conv)
                    db.commit()
                    db.refresh(conv)

                db.add(CrmMessage(
                    conversation_id=conv.id,
                    direction="out",
                    content=texto,
                    sent_by="ai_recovery",
                ))

                # Avança step ou finaliza
                next_step = item.step + 1
                steps_map = {
                    "abandonment":       ABANDONMENT_STEPS,
                    "renewal":           RENEWAL_STEPS,
                    "support":           [timedelta(days=3), timedelta(days=4), timedelta(days=8)],
                    "trial_nurture":     TRIAL_NURTURE_STEPS,
                    "trial_activation":  TRIAL_ACTIVATION_STEPS,
                }
                steps = steps_map.get(item.tipo, [])

                if item.tipo == "trial_expired":
                    # Loop infinito diário — continua até converter ou cancelar
                    item.step = next_step  # avança p/ mudar o ângulo
                    item.next_send_at = now + timedelta(days=1)
                    item.updated_at = now
                elif next_step < len(steps):
                    item.step = next_step
                    item.next_send_at = now + steps[next_step]
                    item.updated_at = now
                else:
                    item.status = "completed"
                    item.updated_at = now

                conv.updated_at = now
                db.commit()
                logger.warning(f"[RECOVERY] Step {item.step} enviado para {item.phone} ({item.tipo}): {texto[:60]}")

            except Exception as e:
                logger.error(f"[RECOVERY] Erro ao enviar para {item.phone}: {e}")
                item.next_send_at = now + timedelta(minutes=30)
                db.commit()

            await asyncio.sleep(2)

    except Exception as e:
        logger.error(f"[RECOVERY] Erro geral: {e}")
    finally:
        db.close()


# ── Fluxo de suporte pós-pagamento ───────────────────────────────────────────

def criar_fila_trial_nurture(phone: str, email: str = "", nome: str = "", db=None):
    """Chamado quando usuário faz cadastro de trial grátis — nurturing de 7 dias para conversão."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        existente = db.query(RecoveryQueue).filter(
            RecoveryQueue.phone == phone,
            RecoveryQueue.tipo == "trial_nurture",
            RecoveryQueue.status == "pending",
        ).first()
        if existente:
            return  # já tem fila ativa

        db.add(RecoveryQueue(
            phone=phone,
            email=email,
            nome=nome,
            tipo="trial_nurture",
            step=0,
            next_send_at=datetime.utcnow() + TRIAL_NURTURE_STEPS[0],
            status="pending",
        ))
        db.commit()
        logger.warning(f"[RECOVERY] Fila trial_nurture criada para {phone}")
    finally:
        if close_db:
            db.close()


def criar_fila_trial_ativacao(phone: str, email: str = "", nome: str = "", db=None):
    """Chamado no cadastro do trial — dispara diariamente até o usuário ativar ou o trial expirar."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        existente = db.query(RecoveryQueue).filter(
            RecoveryQueue.phone == phone,
            RecoveryQueue.tipo == "trial_activation",
            RecoveryQueue.status == "pending",
        ).first()
        if existente:
            return
        db.add(RecoveryQueue(
            phone=phone,
            email=email,
            nome=nome,
            tipo="trial_activation",
            step=0,
            next_send_at=datetime.utcnow() + TRIAL_ACTIVATION_STEPS[0],
            status="pending",
        ))
        db.commit()
        logger.warning(f"[RECOVERY] Fila trial_activation criada para {phone}")
    finally:
        if close_db:
            db.close()


def criar_fila_trial_expirado(phone: str, email: str = "", nome: str = "", db=None):
    """Criada pelo scheduler quando trial expira sem conversão — dispara diariamente sem fim."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        existente = db.query(RecoveryQueue).filter(
            RecoveryQueue.phone == phone,
            RecoveryQueue.tipo == "trial_expired",
            RecoveryQueue.status.in_(["pending", "paused"]),
        ).first()
        if existente:
            return  # já tem fila ativa
        db.add(RecoveryQueue(
            phone=phone,
            email=email,
            nome=nome,
            tipo="trial_expired",
            step=0,
            next_send_at=datetime.utcnow() + timedelta(hours=2),  # primeiro disparo em 2h
            status="pending",
        ))
        db.commit()
        logger.warning(f"[RECOVERY] Fila trial_expired criada para {phone}")
    finally:
        if close_db:
            db.close()


def criar_fila_suporte(phone: str, email: str = "", nome: str = "", db=None):
    """Chamado quando pagamento é confirmado — inicia onboarding."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        # Cancela qualquer fila de abandono/renovação ativa
        for item in db.query(RecoveryQueue).filter(
            RecoveryQueue.phone == phone,
            RecoveryQueue.status.in_(["pending", "paused"]),
        ).all():
            item.status = "cancelled"

        # Cria fila de suporte — dia 3, dia 7, dia 15
        existente = db.query(RecoveryQueue).filter(
            RecoveryQueue.phone == phone,
            RecoveryQueue.tipo == "support",
            RecoveryQueue.status == "pending",
        ).first()
        if not existente:
            db.add(RecoveryQueue(
                phone=phone,
                email=email,
                nome=nome,
                tipo="support",
                step=0,
                next_send_at=datetime.utcnow() + timedelta(days=3),
                status="pending",
            ))
        db.commit()
        logger.warning(f"[RECOVERY] Fila suporte criada para {phone}")
    finally:
        if close_db:
            db.close()
