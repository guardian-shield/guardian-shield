import sys
import os
import asyncio

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from database import engine
from models import Base
from routes import login


@asynccontextmanager
async def lifespan(app):
    from services.crm_followup import run_followup_loop
    task = asyncio.create_task(run_followup_loop())
    yield
    task.cancel()


limiter = Limiter(key_func=get_remote_address)
app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

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
        # tabela garantias criada via Base.metadata mas garante coluna updated_at
        """CREATE TABLE IF NOT EXISTS garantias (
            id SERIAL PRIMARY KEY,
            user_email VARCHAR,
            device_id VARCHAR,
            data_inicio VARCHAR,
            data_fim VARCHAR,
            prazo INTEGER,
            updated_at TIMESTAMP DEFAULT NOW()
        );""",
        """CREATE TABLE IF NOT EXISTS crm_conversations (
            id SERIAL PRIMARY KEY,
            phone VARCHAR,
            contact_name VARCHAR,
            contact_email VARCHAR,
            stage VARCHAR DEFAULT 'lead',
            ai_active BOOLEAN DEFAULT TRUE,
            attendant VARCHAR,
            sector VARCHAR,
            notes TEXT,
            unread INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );""",
        """CREATE TABLE IF NOT EXISTS crm_messages (
            id SERIAL PRIMARY KEY,
            conversation_id INTEGER REFERENCES crm_conversations(id),
            direction VARCHAR,
            content TEXT,
            sent_by VARCHAR,
            wa_message_id VARCHAR,
            sent_at TIMESTAMP DEFAULT NOW()
        );""",
        "CREATE INDEX IF NOT EXISTS idx_crm_conv_phone ON crm_conversations(phone);",
        "CREATE INDEX IF NOT EXISTS idx_crm_msg_conv ON crm_messages(conversation_id);",
        "ALTER TABLE crm_conversations ADD COLUMN IF NOT EXISTS followup_count INTEGER DEFAULT 0;",
        "ALTER TABLE crm_conversations ADD COLUMN IF NOT EXISTS last_followup_at TIMESTAMP;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_usado BOOLEAN DEFAULT FALSE;",
        """CREATE TABLE IF NOT EXISTS recovery_queue (
            id SERIAL PRIMARY KEY,
            phone VARCHAR,
            email VARCHAR,
            nome VARCHAR,
            tipo VARCHAR NOT NULL,
            step INTEGER DEFAULT 0,
            next_send_at TIMESTAMP NOT NULL,
            status VARCHAR DEFAULT 'pending',
            crm_stage VARCHAR,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );""",
        "CREATE INDEX IF NOT EXISTS idx_recovery_phone ON recovery_queue(phone);",
        # Blacklist colaborativa
        """CREATE TABLE IF NOT EXISTS bl_reports (
            id          SERIAL PRIMARY KEY,
            pkg         VARCHAR NOT NULL,
            tech_email  VARCHAR NOT NULL,
            categoria   VARCHAR DEFAULT 'desconhecido',
            reported_at TIMESTAMP DEFAULT NOW()
        );""",
        "CREATE INDEX IF NOT EXISTS idx_bl_reports_pkg ON bl_reports(pkg);",
        "CREATE INDEX IF NOT EXISTS idx_bl_reports_tech ON bl_reports(tech_email);",
        """CREATE TABLE IF NOT EXISTS bl_override (
            pkg        VARCHAR PRIMARY KEY,
            status     VARCHAR NOT NULL,  -- 'aprovado' | 'rejeitado'
            updated_at TIMESTAMP DEFAULT NOW()
        );""",
        # Afiliados
        """CREATE TABLE IF NOT EXISTS affiliates (
            id           SERIAL PRIMARY KEY,
            slug         VARCHAR UNIQUE NOT NULL,
            nome         VARCHAR,
            whatsapp     VARCHAR,
            senha_hash   VARCHAR,
            comissao_pct INTEGER DEFAULT 50,
            ativo        BOOLEAN DEFAULT TRUE,
            created_at   TIMESTAMP DEFAULT NOW()
        );""",
        "CREATE INDEX IF NOT EXISTS idx_affiliates_slug ON affiliates(slug);",
        """CREATE TABLE IF NOT EXISTS affiliate_conversions (
            id               SERIAL PRIMARY KEY,
            affiliate_slug   VARCHAR,
            email_cliente    VARCHAR,
            nome_cliente     VARCHAR,
            whatsapp_cliente VARCHAR,
            plano            VARCHAR,
            valor            INTEGER DEFAULT 0,
            comissao         INTEGER DEFAULT 0,
            payment_id       VARCHAR,
            metodo           VARCHAR,
            created_at       TIMESTAMP DEFAULT NOW()
        );""",
        "CREATE INDEX IF NOT EXISTS idx_aff_conv_slug ON affiliate_conversions(affiliate_slug);",
        # Seed do afiliado profissaosmarti (idempotente)
        """INSERT INTO affiliates (slug, nome, whatsapp, senha_hash, comissao_pct, ativo)
           VALUES ('profissaosmarti', 'Profissão Smarti', '27999806096',
                   '86d3e73560ac60035b80c9319d546e1ced1929a12b6f6ef40d8cef95c24a9680',
                   50, TRUE)
           ON CONFLICT (slug) DO NOTHING;""",
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

from routes import garantias
app.include_router(garantias.router)

from routes import pagamento
app.include_router(pagamento.router)

from routes import crm
app.include_router(crm.router)

from routes import blacklist
app.include_router(blacklist.router)
