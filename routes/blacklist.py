"""
Blacklist colaborativa Guardian Shield
---------------------------------------
POST /blacklist-report   — técnico reporta um ou mais pacotes removidos
GET  /blacklist-community — app busca lista de pkgs com 10+ reportes únicos
GET  /admin/blacklist     — painel admin vê todos os reportes + pode aprovar/rejeitar
POST /admin/blacklist/aprovar   — força entrada na blacklist (< 10 reportes)
POST /admin/blacklist/rejeitar  — rejeita pkg (nunca entra automático)
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session
from sqlalchemy import text

from database import SessionLocal
from auth import verify_token

logger = logging.getLogger("guardian")
router = APIRouter()

THRESHOLD_AUTO = 10   # quantos técnicos únicos para entrar automático


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# POST /blacklist-report
# Recebe lista de pacotes removidos por um técnico e registra
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/blacklist-report")
async def blacklist_report(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(verify_token),
):
    body = await request.json()
    pkgs      = body.get("pkgs", [])
    categoria = body.get("categoria", "desconhecido")  # suspeito | desconhecido
    email     = user.get("sub", "anonimo")

    if not pkgs or not isinstance(pkgs, list):
        return {"error": "pkgs deve ser uma lista"}

    inseridos = 0
    for pkg in pkgs[:50]:   # limite de 50 por chamada
        pkg = str(pkg).strip()
        if not pkg or len(pkg) > 200:
            continue
        # Ignora pacotes do sistema / google / samsung
        if pkg.startswith(("com.google.", "com.android.", "com.samsung.", "android.")):
            continue

        # Verifica se este técnico já reportou este pkg
        existe = db.execute(text(
            "SELECT id FROM bl_reports WHERE pkg = :pkg AND tech_email = :email LIMIT 1"
        ), {"pkg": pkg, "email": email}).fetchone()

        if not existe:
            db.execute(text(
                """INSERT INTO bl_reports (pkg, tech_email, categoria, reported_at)
                   VALUES (:pkg, :email, :cat, :now)"""
            ), {"pkg": pkg, "email": email, "cat": categoria, "now": datetime.utcnow()})
            inseridos += 1

    db.commit()
    logger.info(f"[BL] {email} reportou {inseridos} pacote(s) — categoria: {categoria}")
    return {"status": "ok", "inseridos": inseridos}


# ─────────────────────────────────────────────────────────────────────────────
# GET /blacklist-community
# Retorna lista de pacotes aprovados (automático ≥ threshold ou manual)
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/blacklist-community")
def blacklist_community(db: Session = Depends(get_db)):
    # Pacotes com técnicos únicos >= THRESHOLD_AUTO (excluindo rejeitados)
    rows = db.execute(text(
        """
        SELECT r.pkg, COUNT(DISTINCT r.tech_email) AS total
        FROM bl_reports r
        LEFT JOIN bl_override o ON o.pkg = r.pkg
        WHERE (o.status IS NULL OR o.status != 'rejeitado')
        GROUP BY r.pkg
        HAVING COUNT(DISTINCT r.tech_email) >= :threshold
        UNION
        -- Pacotes aprovados manualmente pelo admin
        SELECT pkg, 999 AS total FROM bl_override WHERE status = 'aprovado'
        """
    ), {"threshold": THRESHOLD_AUTO}).fetchall()

    pkgs = list({row[0] for row in rows})
    return {"pkgs": pkgs, "total": len(pkgs)}


# ─────────────────────────────────────────────────────────────────────────────
# GET /admin/blacklist
# Painel admin — vê ranking de reportes
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/admin/blacklist")
def admin_blacklist(
    db: Session = Depends(get_db),
    x_admin_token: str = Header(None),
):
    from routes.admin import _get_admin_token, DEFAULT_ADMIN_TOKEN
    token_valido = _get_admin_token(db)
    if not x_admin_token or x_admin_token != token_valido:
        return {"error": "Acesso negado"}

    rows = db.execute(text(
        """
        SELECT
            r.pkg,
            COUNT(DISTINCT r.tech_email)         AS tecnicos_unicos,
            COUNT(*)                              AS total_reportes,
            MIN(r.reported_at)                    AS primeiro_reporte,
            MAX(r.reported_at)                    AS ultimo_reporte,
            MODE() WITHIN GROUP (ORDER BY r.categoria) AS categoria,
            o.status                              AS override
        FROM bl_reports r
        LEFT JOIN bl_override o ON o.pkg = r.pkg
        GROUP BY r.pkg, o.status
        ORDER BY tecnicos_unicos DESC
        LIMIT 500
        """
    )).fetchall()

    return {"reportes": [
        {
            "pkg":             row[0],
            "tecnicos_unicos": row[1],
            "total_reportes":  row[2],
            "primeiro_reporte": str(row[3]),
            "ultimo_reporte":   str(row[4]),
            "categoria":        row[5],
            "override":         row[6],
            "auto_blacklist":   row[1] >= THRESHOLD_AUTO,
        }
        for row in rows
    ], "threshold": THRESHOLD_AUTO}


# ─────────────────────────────────────────────────────────────────────────────
# POST /admin/blacklist/aprovar
# Força entrada mesmo com < 10 reportes
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/admin/blacklist/aprovar")
def admin_bl_aprovar(
    pkg: str,
    db: Session = Depends(get_db),
    x_admin_token: str = Header(None),
):
    from routes.admin import _get_admin_token
    if not x_admin_token or x_admin_token != _get_admin_token(db):
        return {"error": "Acesso negado"}

    db.execute(text(
        """INSERT INTO bl_override (pkg, status, updated_at)
           VALUES (:pkg, 'aprovado', :now)
           ON CONFLICT (pkg) DO UPDATE SET status='aprovado', updated_at=:now"""
    ), {"pkg": pkg, "now": datetime.utcnow()})
    db.commit()
    logger.warning(f"[BL] Admin aprovou manualmente: {pkg}")
    return {"status": "aprovado", "pkg": pkg}


# ─────────────────────────────────────────────────────────────────────────────
# POST /admin/blacklist/rejeitar
# Impede que pkg entre na blacklist mesmo com muitos reportes
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/admin/blacklist/rejeitar")
def admin_bl_rejeitar(
    pkg: str,
    db: Session = Depends(get_db),
    x_admin_token: str = Header(None),
):
    from routes.admin import _get_admin_token
    if not x_admin_token or x_admin_token != _get_admin_token(db):
        return {"error": "Acesso negado"}

    db.execute(text(
        """INSERT INTO bl_override (pkg, status, updated_at)
           VALUES (:pkg, 'rejeitado', :now)
           ON CONFLICT (pkg) DO UPDATE SET status='rejeitado', updated_at=:now"""
    ), {"pkg": pkg, "now": datetime.utcnow()})
    db.commit()
    logger.warning(f"[BL] Admin rejeitou: {pkg}")
    return {"status": "rejeitado", "pkg": pkg}
