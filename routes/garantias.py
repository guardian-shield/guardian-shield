from datetime import datetime
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Garantia
from auth import verify_token

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# =============================================================
# GET /garantias  →  retorna todas as garantias do usuário
# =============================================================
@router.get("/garantias")
def listar_garantias(user=Depends(verify_token), db: Session = Depends(get_db)):
    email = user.get("sub")
    rows = db.query(Garantia).filter(Garantia.user_email == email).all()
    resultado = {}
    for g in rows:
        resultado[g.device_id] = {
            "dataInicio": g.data_inicio,
            "dataFim":    g.data_fim,
            "prazo":      g.prazo,
        }
    return resultado


# =============================================================
# POST /garantias/sync  →  recebe dict local e salva/atualiza
# =============================================================
@router.post("/garantias/sync")
def sincronizar_garantias(
    payload: dict,
    user=Depends(verify_token),
    db: Session = Depends(get_db),
):
    """
    payload: { "device_id": { "dataInicio": "...", "dataFim": "...", "prazo": N }, ... }
    Upsert de todos os registros — nunca mistura dados de usuários diferentes.
    """
    email = user.get("sub")

    for device_id, dados in payload.items():
        row = db.query(Garantia).filter(
            Garantia.user_email == email,
            Garantia.device_id  == device_id,
        ).first()

        if row:
            row.data_inicio = dados.get("dataInicio", row.data_inicio)
            row.data_fim    = dados.get("dataFim",    row.data_fim)
            row.prazo       = dados.get("prazo",      row.prazo)
            row.updated_at  = datetime.utcnow()
        else:
            db.add(Garantia(
                user_email  = email,
                device_id   = device_id,
                data_inicio = dados.get("dataInicio"),
                data_fim    = dados.get("dataFim"),
                prazo       = dados.get("prazo"),
            ))

    db.commit()
    return {"status": "ok", "sincronizados": len(payload)}
