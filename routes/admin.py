import os
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User, AppConfig, MessageLog
from auth import hash_password
from services.email_service import send_email
from services.whatsapp_service import send_whatsapp_message

router = APIRouter()

DEFAULT_ADMIN_TOKEN = "Manu1016+"


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
        user.nome             = nome or user.nome
        user.password         = hash_password(password)
        user.whatsapp         = whatsapp or user.whatsapp
        user.email_verified   = True   # admin já valida
        user.pre_liberado     = False
        if expires:
            user.expires_at   = expires
            user.plan_type    = "manual"
    else:
        user = User(
            nome           = nome,
            email          = email,
            password       = hash_password(password),
            whatsapp       = whatsapp,
            email_verified = True,   # admin já valida
            expires_at     = expires,
            plan_type      = "manual" if expires else None,
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
        if key in allowed and value is not None:
            _cfg_set(db, key, str(value))

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
