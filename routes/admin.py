import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User, AppConfig, MessageLog
from auth import hash_password
from services.email_service import send_email
from services.whatsapp_service import send_whatsapp_message

router = APIRouter()

DEFAULT_ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "Manu1016+")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_admin_token(db: Session) -> str:
    try:
        row = db.query(AppConfig).filter(AppConfig.key == "admin_token").first()
        return row.value if row and row.value else DEFAULT_ADMIN_TOKEN
    except Exception:
        return DEFAULT_ADMIN_TOKEN


def verificar_admin(x_admin_token: str = Header(None), db: Session = Depends(get_db)):
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Acesso negado")
    token_valido = _get_admin_token(db)
    if x_admin_token != token_valido:
        raise HTTPException(status_code=401, detail="Acesso negado")


def _cfg_get(db: Session, key: str, default=None):
    row = db.query(AppConfig).filter(AppConfig.key == key).first()
    return row.value if row and row.value else default


def _cfg_set(db: Session, key: str, value: str):
    row = db.query(AppConfig).filter(AppConfig.key == key).first()
    if row:
        row.value = value
    else:
        db.add(AppConfig(key=key, value=value))


# =============================================================
# GET /admin  →  serve o painel HTML
# =============================================================
@router.get("/admin", response_class=HTMLResponse)
def admin_panel():
    html_path = os.path.join(os.path.dirname(__file__), "..", "templates", "admin.html")
    with open(os.path.abspath(html_path), encoding="utf-8") as f:
        return f.read()


# =============================================================
# GET /admin/users
# =============================================================
@router.get("/admin/users")
def listar_usuarios(db: Session = Depends(get_db), admin=Depends(verificar_admin)):
    users = db.query(User).order_by(User.created_at.desc()).all()
    resultado = []
    for u in users:
        ativo = bool(u.expires_at and u.expires_at > datetime.utcnow())
        resultado.append({
            "id":                u.id,
            "nome":              u.nome,
            "email":             u.email,
            "whatsapp":          u.whatsapp,
            "plano":             u.plan_type,
            "ativo":             ativo,
            "expira_em":         u.expires_at,
            "hwid_1":            u.hwid_1,
            "hwid_2":            u.hwid_2,
            "email_verified":    u.email_verified,
            "whatsapp_verified": u.whatsapp_verified,
            "pre_liberado":      u.pre_liberado,
            "created_at":        u.created_at,
        })
    return resultado


# =============================================================
# POST /admin/ativar
# =============================================================
@router.post("/admin/ativar")
def ativar_usuario(
    email: str,
    dias: int = 30,
    plano: str = "",
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin),
):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "Usuário não encontrado"}
    user.expires_at = datetime.utcnow() + timedelta(days=dias)
    # Define plano: usa o parâmetro se informado, ou infere pelos dias, ou mantém "manual"
    if plano:
        user.plan_type = plano
    elif dias >= 300:
        user.plan_type = "anual"
    elif dias >= 25:
        user.plan_type = "teste"
    else:
        user.plan_type = "manual"
    db.commit()
    return {"status": "ativado", "expira_em": user.expires_at, "plan_type": user.plan_type}


# =============================================================
# POST /admin/desativar
# =============================================================
@router.post("/admin/desativar")
def desativar_usuario(
    email: str,
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin),
):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "Usuário não encontrado"}
    user.expires_at = datetime.utcnow()
    db.commit()
    return {"status": "desativado"}


# =============================================================
# POST /admin/forcar-verificacao
# =============================================================
@router.post("/admin/forcar-verificacao")
def forcar_verificacao(
    email: str,
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin),
):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "Usuário não encontrado"}
    user.email_verified    = True
    user.whatsapp_verified = True
    db.commit()
    return {"status": "verificado", "email": email}


# =============================================================
# POST /admin/reenviar-codigo-wa
# Reenvia código WhatsApp sem precisar ir no banco
# =============================================================
@router.post("/admin/reenviar-codigo-wa")
def reenviar_codigo_wa(
    email: str,
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin),
):
    import random
    from datetime import timedelta
    from services.whatsapp_service import send_verification_whatsapp

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "Usuário não encontrado"}
    if not user.whatsapp:
        return {"error": f"Usuário não tem WhatsApp cadastrado. Cadastre pelo painel antes de reenviar."}

    code = str(random.randint(100000, 999999))
    user.whatsapp_code         = code
    user.whatsapp_code_expires = datetime.utcnow() + timedelta(minutes=15)
    user.whatsapp_verified     = False
    db.commit()

    try:
        send_verification_whatsapp(user.whatsapp, user.nome or email, code, db)
        return {"status": "enviado", "whatsapp": user.whatsapp, "code_preview": code[:2] + "****"}
    except Exception as e:
        return {"error": f"Falha ao enviar: {str(e)}", "whatsapp": user.whatsapp}


# =============================================================
# POST /admin/atualizar-whatsapp
# Atualiza o WhatsApp de um usuário e reenvia o código
# =============================================================
@router.post("/admin/atualizar-whatsapp")
def atualizar_whatsapp(
    email: str,
    whatsapp: str,
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin),
):
    import random
    from datetime import timedelta
    from services.whatsapp_service import send_verification_whatsapp

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "Usuário não encontrado"}

    user.whatsapp          = whatsapp
    user.whatsapp_verified = False
    code = str(random.randint(100000, 999999))
    user.whatsapp_code         = code
    user.whatsapp_code_expires = datetime.utcnow() + timedelta(minutes=15)
    db.commit()

    try:
        send_verification_whatsapp(whatsapp, user.nome or email, code, db)
        return {"status": "whatsapp_atualizado_e_codigo_enviado", "whatsapp": whatsapp}
    except Exception as e:
        return {"error": f"WhatsApp atualizado mas falha ao enviar código: {str(e)}"}


# =============================================================
# POST /admin/liberar-login
# Remove flag pre_liberado para usuários travados no cadastro (ex: trial grátis)
# =============================================================
@router.post("/admin/liberar-login")
def liberar_login(
    email: str,
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin),
):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "Usuário não encontrado"}
    user.pre_liberado = False
    db.commit()
    return {"status": "liberado", "email": email, "pre_liberado": False}


# POST /admin/reset-hwid
# =============================================================
@router.post("/admin/reset-hwid")
def reset_hwid(
    email: str,
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin),
):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "Usuário não encontrado"}
    user.hwid_1 = None
    user.hwid_2 = None
    db.commit()
    return {"status": "hwid resetado"}


# =============================================================
# DELETE /admin/deletar
# =============================================================
@router.delete("/admin/deletar")
def deletar_usuario(
    email: str,
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin),
):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "Usuário não encontrado"}
    db.delete(user)
    db.commit()
    return {"status": "deletado", "email": email}


# =============================================================
# POST /admin/cadastrar  →  admin cria usuário direto
# =============================================================
@router.post("/admin/cadastrar")
def cadastrar_usuario(
    email: str,
    password: str,
    nome: str = "",
    whatsapp: str = "",
    dias: int = 0,
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin),
):
    user = db.query(User).filter(User.email == email).first()
    if user and not user.pre_liberado:
        return {"error": "E-mail já cadastrado"}

    expires = datetime.utcnow() + timedelta(days=dias) if dias > 0 else None

    if user and user.pre_liberado:
        user.nome               = nome or user.nome
        user.password           = hash_password(password)
        user.whatsapp           = whatsapp or user.whatsapp
        user.email_verified     = True   # admin já valida
        user.whatsapp_verified  = True   # admin já valida
        user.pre_liberado       = False
        if expires:
            user.expires_at     = expires
            user.plan_type      = "manual"
    else:
        user = User(
            nome               = nome,
            email              = email,
            password           = hash_password(password),
            whatsapp           = whatsapp,
            email_verified     = True,   # admin já valida
            whatsapp_verified  = True,   # admin já valida
            expires_at         = expires,
            plan_type          = "manual" if expires else None,
        )
        db.add(user)

    db.commit()
    return {"status": "cadastrado", "email": email}


# =============================================================
# POST /admin/pre-liberar  →  libera email antes do cadastro
# =============================================================
@router.post("/admin/pre-liberar")
def pre_liberar(
    email: str,
    dias: int = 30,
    plano: str = "manual",
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin),
):
    user = db.query(User).filter(User.email == email).first()
    expires = datetime.utcnow() + timedelta(days=dias)
    if user:
        user.expires_at  = expires
        user.plan_type   = plano
        user.pre_liberado = True
    else:
        user = User(email=email, pre_liberado=True, expires_at=expires, plan_type=plano)
        db.add(user)
    db.commit()
    return {"status": "pre_liberado", "email": email}


# =============================================================
# GET /admin/config
# =============================================================
@router.get("/admin/config")
def get_config(db: Session = Depends(get_db), admin=Depends(verificar_admin)):
    keys = [
        "email_provider", "resend_api_key", "resend_from",
        "gmail_email", "gmail_password",
        "evolution_api_url", "evolution_api_key", "evolution_instance",
        "mp_token",
    ]
    return {k: _cfg_get(db, k) for k in keys}


# =============================================================
# POST /admin/config
# =============================================================
@router.post("/admin/config")
def save_config(payload: dict, db: Session = Depends(get_db), admin=Depends(verificar_admin)):
    # Troca de token admin
    token_novo  = payload.pop("admin_token_novo", None)
    token_atual = payload.pop("admin_token_atual", None)
    token_atualizado = False

    if token_novo:
        token_valido = _get_admin_token(db)
        if token_atual != token_valido:
            return {"error": "Token atual incorreto"}
        _cfg_set(db, "admin_token", token_novo)
        token_atualizado = True

    # Salva demais configurações
    allowed = {
        "email_provider", "resend_api_key", "resend_from",
        "gmail_email", "gmail_password",
        "evolution_api_url", "evolution_api_key", "evolution_instance",
        "mp_token",
    }
    for key, value in payload.items():
        if key in allowed and value is not None and str(value).strip() != "":
            _cfg_set(db, key, str(value).strip())

    db.commit()
    return {"status": "salvo", "token_atualizado": token_atualizado}


# =============================================================
# POST /admin/send-message
# =============================================================
@router.post("/admin/send-message")
def send_message(payload: dict, db: Session = Depends(get_db), admin=Depends(verificar_admin)):
    email    = payload.get("email", "todos")
    canal    = payload.get("canal", "whatsapp")
    mensagem = payload.get("mensagem", "").strip()

    if not mensagem:
        return {"error": "Mensagem vazia"}

    destinatarios = []
    if email == "todos":
        users = db.query(User).filter(
            User.expires_at > datetime.utcnow(),
            User.email_verified == True,
        ).all()
        destinatarios = users
    else:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return {"error": "Usuário não encontrado"}
        destinatarios = [user]

    enviados = 0
    for u in destinatarios:
        status = "failed"
        error  = None
        try:
            if canal == "whatsapp" and u.whatsapp:
                send_whatsapp_message(u.whatsapp, mensagem, db)
                status = "sent"
            elif canal == "email":
                html_msg = f"<div style='font-family:Arial,sans-serif;padding:20px'>{mensagem.replace(chr(10),'<br>')}</div>"
                send_email(u.email, "Guardian Shield — Mensagem", html_msg, db)
                status = "sent"
        except Exception as e:
            error = str(e)

        log = MessageLog(
            user_email = u.email,
            user_nome  = u.nome,
            message    = mensagem,
            channel    = canal,
            status     = status,
            error      = error,
        )
        db.add(log)
        if status == "sent":
            enviados += 1

    db.commit()
    return {"status": "ok", "enviados": enviados, "total": len(destinatarios)}


# =============================================================
# GET /admin/messages
# =============================================================
@router.post("/admin/crm-lead")
async def crm_lead_manual(request: Request, db: Session = Depends(get_db), admin=Depends(verificar_admin)):
    """Cria lead no CRM e dispara primeira mensagem da Maia."""
    from models import CrmConversation, CrmMessage
    from services.crm_ai import get_ai_response, needs_human, clean_response
    import time

    data = await request.json()
    phone = data.get("phone", "").replace("+","").replace(" ","").replace("-","")
    email = data.get("email","")
    msg_contexto = data.get("mensagem_contexto","")

    if not phone:
        return {"error": "phone obrigatório"}

    # Cria usuário se não existir
    from models import User
    user = db.query(User).filter(User.email == email).first() if email else None
    if email and not user:
        user = User(email=email, whatsapp=phone)
        db.add(user)
        db.commit()

    # Cria ou atualiza conversa
    conv = db.query(CrmConversation).filter(CrmConversation.phone == phone).first()
    if not conv:
        conv = CrmConversation(phone=phone, contact_email=email, stage="initiated", ai_active=True)
        db.add(conv)
        db.commit()
        db.refresh(conv)
    else:
        conv.stage = "initiated"
        conv.ai_active = True
        if email and not conv.contact_email:
            conv.contact_email = email
        db.commit()

    # Registra contexto
    if msg_contexto:
        db.add(CrmMessage(conversation_id=conv.id, direction="out", content=f"[Sistema] {msg_contexto}", sent_by="system"))
        db.commit()

    # Gera e envia mensagem inicial da Maia com o contexto
    history = [{"direction": "out", "content": f"[Sistema] {msg_contexto}", "sent_at": None}]
    user_trigger = msg_contexto  # usamos como gatilho interno
    ai_text = get_ai_response([], user_trigger)
    if ai_text:
        reply = clean_response(ai_text)
        time.sleep(2)
        try:
            send_whatsapp_message(phone, reply, db)
            db.add(CrmMessage(conversation_id=conv.id, direction="out", content=reply, sent_by="ai"))
            db.commit()
        except Exception as e:
            return {"status": "lead criado mas falha ao enviar mensagem", "error": str(e)}

    return {"status": "ok", "conv_id": conv.id, "phone": phone}


@router.get("/admin/messages")
def get_messages(db: Session = Depends(get_db), admin=Depends(verificar_admin)):
    logs = db.query(MessageLog).order_by(MessageLog.sent_at.desc()).limit(200).all()
    return [
        {
            "id":         l.id,
            "user_email": l.user_email,
            "user_nome":  l.user_nome,
            "message":    l.message,
            "channel":    l.channel,
            "sent_at":    l.sent_at,
            "status":     l.status,
            "error":      l.error,
        }
        for l in logs
    ]


# ─── API: enfileirar lead na recuperação manualmente ──────────────────────────

@router.post("/admin/recovery-enqueue")
async def recovery_enqueue(request: Request, db: Session = Depends(get_db), admin=Depends(verificar_admin)):
    data = await request.json()
    phone = data.get("phone", "").replace("+", "").replace(" ", "").replace("-", "")
    email = data.get("email", "")
    nome  = data.get("nome", "")
    tipo  = data.get("tipo", "abandonment")

    if not phone:
        return {"error": "phone obrigatório"}

    from services.recovery_service import criar_fila_abandono, criar_fila_renovacao
    if tipo == "renewal":
        criar_fila_renovacao(phone=phone, email=email, nome=nome, dias_para_vencer=0, db=db)
    else:
        criar_fila_abandono(phone=phone, email=email, nome=nome, db=db)

    return {"ok": True, "phone": phone, "tipo": tipo}


# =============================================================
# GET /admin/dashboard  →  dados do painel analítico
# =============================================================
@router.get("/admin/dashboard")
def get_dashboard(periodo: int = 30, db: Session = Depends(get_db), admin=Depends(verificar_admin)):
    from models import CrmConversation, AffiliateConversion, Affiliate
    from collections import Counter

    now = datetime.utcnow()
    hoje_inicio = datetime(now.year, now.month, now.day)
    mes_inicio  = datetime(now.year, now.month, 1)

    PLANO_VALOR = {
        "anual":    29900,
        "anual79":   7990,
        "anual199": 19900,
        "teste":     4990,
        "mensal":    4990,
    }
    PLANOS_PAGOS = set(PLANO_VALOR.keys())

    all_users = db.query(User).all()

    # ── BLOCO 1: Cards ────────────────────────────────────────
    ativos         = [u for u in all_users if u.expires_at and u.expires_at > now]
    trials_ativos  = [u for u in ativos if u.plan_type == "trial_gratis"]
    pagos_ativos   = [u for u in ativos if u.plan_type in PLANOS_PAGOS]
    expirando_7d   = [u for u in ativos if (u.expires_at - now).days <= 7]

    cadastros_hoje = sum(1 for u in all_users if u.created_at and u.created_at >= hoje_inicio)
    cadastros_mes  = sum(1 for u in all_users if u.created_at and u.created_at >= mes_inicio)

    # Receita: AffiliateConversion (exato) + estimativa para não-afiliados
    conv_mes = db.query(AffiliateConversion).filter(AffiliateConversion.created_at >= mes_inicio).all()
    receita_afiliados = sum(c.valor for c in conv_mes)
    emails_aff_mes    = {c.email_cliente for c in conv_mes}
    nao_afilados_mes  = [
        u for u in all_users
        if u.created_at and u.created_at >= mes_inicio
        and u.plan_type in PLANOS_PAGOS
        and u.email not in emails_aff_mes
    ]
    receita_estimada   = sum(PLANO_VALOR.get(u.plan_type, 0) for u in nao_afilados_mes)
    receita_total_mes  = receita_afiliados + receita_estimada

    total_trials   = sum(1 for u in all_users if u.trial_usado)
    convertidos    = sum(1 for u in all_users if u.trial_usado and u.plan_type in PLANOS_PAGOS)
    taxa_conversao = round(convertidos / max(total_trials, 1) * 100, 1)

    cards = {
        "receita_mes_cents":  receita_total_mes,
        "cadastros_hoje":     cadastros_hoje,
        "cadastros_mes":      cadastros_mes,
        "ativos_total":       len(ativos),
        "trials_ativos":      len(trials_ativos),
        "pagos_ativos":       len(pagos_ativos),
        "expirando_7d":       len(expirando_7d),
        "taxa_conversao_pct": taxa_conversao,
        "total_usuarios":     len(all_users),
    }

    # ── BLOCO 2: Funil de aquisição ───────────────────────────
    total_leads     = db.query(CrmConversation).count()
    total_cadastros = len(all_users)
    com_hwid        = sum(1 for u in all_users if u.hwid_1)
    total_pagos     = sum(1 for u in all_users if u.plan_type in PLANOS_PAGOS)

    funil_aquisicao = [
        {"label": "Leads CRM",          "valor": total_leads},
        {"label": "Cadastros",           "valor": total_cadastros},
        {"label": "App ativado (HWID)",  "valor": com_hwid},
        {"label": "Cliente pago",        "valor": total_pagos},
    ]

    # ── BLOCO 3: Crescimento por dia ──────────────────────────
    all_convs_aff = db.query(AffiliateConversion).filter(
        AffiliateConversion.created_at >= (now - timedelta(days=periodo))
    ).all()

    dias_labels, dias_cadastros, dias_vendas, dias_receita = [], [], [], []
    for i in range(periodo - 1, -1, -1):
        d        = now - timedelta(days=i)
        d_inicio = datetime(d.year, d.month, d.day)
        d_fim    = d_inicio + timedelta(days=1)
        dias_labels.append(d.strftime("%d/%m"))

        cad = sum(1 for u in all_users if u.created_at and d_inicio <= u.created_at < d_fim)
        dias_cadastros.append(cad)

        aff_dia    = [c for c in all_convs_aff if c.created_at and d_inicio <= c.created_at < d_fim]
        emails_aff = {c.email_cliente for c in aff_dia}
        naff_dia   = [
            u for u in all_users
            if u.created_at and d_inicio <= u.created_at < d_fim
            and u.plan_type in PLANOS_PAGOS
            and u.email not in emails_aff
        ]
        dias_vendas.append(len(aff_dia) + len(naff_dia))
        dias_receita.append(round(
            (sum(c.valor for c in aff_dia) + sum(PLANO_VALOR.get(u.plan_type, 0) for u in naff_dia)) / 100, 2
        ))

    crescimento = {
        "labels":    dias_labels,
        "cadastros": dias_cadastros,
        "vendas":    dias_vendas,
        "receita":   dias_receita,
    }

    # ── BLOCO 4: Distribuição de planos ───────────────────────
    planos_count = Counter(u.plan_type or "sem_plano" for u in ativos)
    planos_dist  = [{"plano": k, "total": v} for k, v in sorted(planos_count.items(), key=lambda x: -x[1])]

    # ── BLOCO 5: Funil do trial ───────────────────────────────
    todos_trials      = [u for u in all_users if u.trial_usado or u.plan_type == "trial_gratis"]
    trial_wa          = sum(1 for u in todos_trials if u.whatsapp_verified)
    trial_hwid        = sum(1 for u in todos_trials if u.hwid_1)
    trial_conv        = sum(1 for u in todos_trials if u.plan_type in PLANOS_PAGOS)
    trial_exp_sem     = sum(1 for u in todos_trials if u.plan_type == "trial_gratis" and u.expires_at and u.expires_at < now)
    n_trial           = max(len(todos_trials), 1)

    funil_trial = [
        {"label": "Trial cadastrado",       "valor": len(todos_trials), "pct": 100},
        {"label": "WhatsApp verificado",    "valor": trial_wa,          "pct": round(trial_wa   / n_trial * 100, 1)},
        {"label": "App baixado (HWID)",     "valor": trial_hwid,        "pct": round(trial_hwid / n_trial * 100, 1)},
        {"label": "Converteu para pago",    "valor": trial_conv,        "pct": round(trial_conv / n_trial * 100, 1)},
        {"label": "Expirou sem converter",  "valor": trial_exp_sem,     "pct": round(trial_exp_sem / n_trial * 100, 1)},
    ]

    # ── BLOCO 6: Churn ────────────────────────────────────────
    expirando_30d = sorted([
        {
            "nome":           u.nome or u.email,
            "email":          u.email,
            "plano":          u.plan_type,
            "expira_em":      u.expires_at.isoformat(),
            "dias_restantes": (u.expires_at - now).days,
        }
        for u in ativos if (u.expires_at - now).days <= 30
    ], key=lambda x: x["dias_restantes"])

    perdidos_30d = sum(
        1 for u in all_users
        if u.expires_at and u.expires_at < (now - timedelta(days=30))
        and u.plan_type in PLANOS_PAGOS
    )

    # ── BLOCO 7: CRM ──────────────────────────────────────────
    all_convs   = db.query(CrmConversation).all()
    stages_cnt  = Counter(c.stage or "lead" for c in all_convs)
    ia_ativa    = sum(1 for c in all_convs if c.ai_active)
    transferidas= sum(1 for c in all_convs if not c.ai_active)

    leads_dia = []
    labels_30d = []
    for i in range(29, -1, -1):
        d        = now - timedelta(days=i)
        d_inicio = datetime(d.year, d.month, d.day)
        d_fim    = d_inicio + timedelta(days=1)
        labels_30d.append(d.strftime("%d/%m"))
        leads_dia.append(sum(1 for c in all_convs if c.created_at and d_inicio <= c.created_at < d_fim))

    crm = {
        "total_leads":  len(all_convs),
        "ia_ativa":     ia_ativa,
        "transferidas": transferidas,
        "stages":       [{"stage": k, "total": v} for k, v in stages_cnt.items()],
        "leads_dia":    leads_dia,
        "labels_30d":   labels_30d,
    }

    # ── BLOCO 8: Afiliados ────────────────────────────────────
    afiliados_list = []
    for aff in db.query(Affiliate).filter(Affiliate.ativo == True).all():
        all_aff_conv = db.query(AffiliateConversion).filter(AffiliateConversion.affiliate_slug == aff.slug).all()
        mes_aff_conv = [c for c in all_aff_conv if c.created_at and c.created_at >= mes_inicio]
        afiliados_list.append({
            "slug":          aff.slug,
            "nome":          aff.nome or aff.slug,
            "total_vendas":  len(all_aff_conv),
            "vendas_mes":    len(mes_aff_conv),
            "receita_total": sum(c.valor for c in all_aff_conv) / 100,
            "receita_mes":   sum(c.valor for c in mes_aff_conv) / 100,
            "comissao_mes":  sum(c.comissao for c in mes_aff_conv) / 100,
        })
    afiliados_list.sort(key=lambda x: -x["receita_total"])

    return {
        "cards":           cards,
        "funil_aquisicao": funil_aquisicao,
        "crescimento":     crescimento,
        "planos_dist":     planos_dist,
        "funil_trial":     funil_trial,
        "churn":           {"expirando_30d": expirando_30d, "perdidos_30d": perdidos_30d},
        "crm":             crm,
        "afiliados":       afiliados_list,
    }
