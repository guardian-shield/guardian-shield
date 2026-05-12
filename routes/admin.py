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
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin),
):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"error": "Usuário não encontrado"}
    user.expires_at = datetime.utcnow() + timedelta(days=dias)
    if not user.plan_type:
        user.plan_type = "manual"
    db.commit()
    return {"status": "ativado", "expira_em": user.expires_at}


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
