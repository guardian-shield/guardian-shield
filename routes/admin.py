from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User
from datetime import datetime, timedelta

router = APIRouter()

# 🔐 TOKEN ADMIN
ADMIN_TOKEN = "Manu1016+"

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 🔥 PROTEÇÃO REAL (AGORA BLOQUEIA)
def verificar_admin(x_admin_token: str = Header(None)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Acesso negado")

# =========================
# LISTAR USUÁRIOS
# =========================
@router.get("/admin/users")
def listar_usuarios(
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin)
):
    users = db.query(User).all()

    resultado = []

    for u in users:
        ativo = False
        if u.expires_at and u.expires_at > datetime.utcnow():
            ativo = True

        resultado.append({
            "email": u.email,
            "plano": u.plan_type,
            "ativo": ativo,
            "expira_em": u.expires_at,
            "hwid_1": u.hwid_1,
            "hwid_2": u.hwid_2
        })

    return resultado

# =========================
# ATIVAR USUÁRIO
# =========================
@router.post("/admin/ativar")
def ativar_usuario(
    email: str,
    dias: int = 30,
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin)
):
    user = db.query(User).filter(User.email == email).first()

    if not user:
        return {"error": "Usuário não encontrado"}

    user.expires_at = datetime.utcnow() + timedelta(days=dias)
    db.commit()

    return {"status": "ativado"}

# =========================
# RESETAR HWID
# =========================
@router.post("/admin/reset-hwid")
def reset_hwid(
    email: str,
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin)
):
    user = db.query(User).filter(User.email == email).first()

    if not user:
        return {"error": "Usuário não encontrado"}

    user.hwid_1 = None
    user.hwid_2 = None
    db.commit()

    return {"status": "hwid resetado"}

# =========================
# DESATIVAR USUÁRIO
# =========================
@router.post("/admin/desativar")
def desativar_usuario(
    email: str,
    db: Session = Depends(get_db),
    admin=Depends(verificar_admin)
):
    user = db.query(User).filter(User.email == email).first()

    if not user:
        return {"error": "Usuário não encontrado"}

    user.expires_at = datetime.utcnow()
    db.commit()

    return {"status": "desativado"}