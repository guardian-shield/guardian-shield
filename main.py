import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import traceback
from fastapi import FastAPI
from database import engine
from models import Base
from routes import login

app = FastAPI()

# Cria tabelas novas (app_config, message_logs)
Base.metadata.create_all(bind=engine)


def migrar_banco():
    """Adiciona colunas novas na tabela users sem perder dados existentes."""
    novos_campos = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS nome VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS whatsapp VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS whatsapp_verified BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_code VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_code_expires TIMESTAMP;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS whatsapp_code VARCHAR;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS whatsapp_code_expires TIMESTAMP;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS pre_liberado BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();",
    ]
    with engine.connect() as conn:
        for sql in novos_campos:
            try:
                conn.execute(__import__('sqlalchemy').text(sql))
            except Exception as e:
                print(f"[MIGRAÇÃO] {sql[:60]}... → {e}")
        conn.commit()
    print("[MIGRAÇÃO] Concluída.")


migrar_banco()

app.include_router(login.router)


@app.get("/")
def home():
    return {"status": "Guardian Shield API rodando"}


from routes import admin
app.include_router(admin.router)
