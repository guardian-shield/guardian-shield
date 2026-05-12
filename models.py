from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.sql import func
from database import Base


class User(Base):
    __tablename__ = "users"

    id                   = Column(Integer, primary_key=True, index=True)
    nome                 = Column(String, nullable=True)
    email                = Column(String, unique=True, index=True)
    password             = Column(String, nullable=True)
    whatsapp             = Column(String, nullable=True)
    plan_type            = Column(String, nullable=True)
    expires_at           = Column(DateTime, nullable=True)
    hwid_1               = Column(String, nullable=True)
    hwid_2               = Column(String, nullable=True)
    email_verified       = Column(Boolean, default=False)
    whatsapp_verified    = Column(Boolean, default=False)
    email_code           = Column(String, nullable=True)
    email_code_expires   = Column(DateTime, nullable=True)
    whatsapp_code        = Column(String, nullable=True)
    whatsapp_code_expires = Column(DateTime, nullable=True)
    pre_liberado         = Column(Boolean, default=False)
    trial_usado          = Column(Boolean, default=False)
    created_at           = Column(DateTime, default=func.now())


class AppConfig(Base):
    """Configurações do sistema — chave/valor editáveis pelo painel admin."""
    __tablename__ = "app_config"

    id    = Column(Integer, primary_key=True)
    key   = Column(String, unique=True, index=True)
    value = Column(Text, nullable=True)


class MessageLog(Base):
    """Histórico de mensagens enviadas pelo painel admin."""
    __tablename__ = "message_logs"

    id         = Column(Integer, primary_key=True)
    user_email = Column(String)
    user_nome  = Column(String, nullable=True)
    message    = Column(Text)
    channel    = Column(String)   # 'whatsapp' | 'email'
    sent_at    = Column(DateTime, default=func.now())
    status     = Column(String)   # 'sent' | 'failed'
    error      = Column(Text, nullable=True)


class CrmConversation(Base):
    """Conversa CRM — uma por contato WhatsApp."""
    __tablename__ = "crm_conversations"

    id            = Column(Integer, primary_key=True, index=True)
    phone         = Column(String, index=True)          # ex: 5545999999999
    contact_name  = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    # Kanban: lead | initiated | paid | active | expiring | cancelled | support
    stage         = Column(String, default="lead")
    ai_active     = Column(Boolean, default=True)       # IA respondendo?
    attendant     = Column(String, nullable=True)       # nome do atendente
    sector        = Column(String, nullable=True)       # setor
    notes         = Column(Text, nullable=True)         # anotações internas
    unread        = Column(Integer, default=0)          # msgs não lidas
    followup_count = Column(Integer, default=0)         # quantos follow-ups enviados
    last_followup_at = Column(DateTime, nullable=True)  # última vez que enviou follow-up
    created_at    = Column(DateTime, default=func.now())
    updated_at    = Column(DateTime, default=func.now(), onupdate=func.now())


class CrmMessage(Base):
    """Mensagem de uma conversa CRM."""
    __tablename__ = "crm_messages"

    id              = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("crm_conversations.id"), index=True)
    direction       = Column(String)   # 'in' | 'out'
    content         = Column(Text)
    sent_by         = Column(String, nullable=True)  # 'ai' | 'system' | nome do atendente
    wa_message_id   = Column(String, nullable=True)  # ID do WhatsApp para dedup
    sent_at         = Column(DateTime, default=func.now())


class RecoveryQueue(Base):
    """Fila de recuperação — abandono de pagamento e não-renovação."""
    __tablename__ = "recovery_queue"

    id            = Column(Integer, primary_key=True, index=True)
    phone         = Column(String, index=True)       # número do lead
    email         = Column(String, nullable=True)
    nome          = Column(String, nullable=True)
    # Tipo: abandonment (PIX/cartão abandonado) | renewal (não renovou)
    tipo          = Column(String, nullable=False)
    # Estágio atual da fila (0=primeiro disparo, 1=segundo, etc.)
    step          = Column(Integer, default=0)
    # Quando enviar o próximo disparo
    next_send_at  = Column(DateTime, nullable=False)
    # Status: pending | paused | cancelled | completed
    status        = Column(String, default="pending")
    # Stage do CRM no momento da criação (para contexto)
    crm_stage     = Column(String, nullable=True)
    created_at    = Column(DateTime, default=func.now())
    updated_at    = Column(DateTime, default=func.now(), onupdate=func.now())


class Affiliate(Base):
    """Afiliado — influenciador ou parceiro com link próprio."""
    __tablename__ = "affiliates"

    id         = Column(Integer, primary_key=True, index=True)
    slug       = Column(String, unique=True, index=True)   # ex: profissaosmarti
    nome       = Column(String, nullable=True)
    whatsapp   = Column(String, nullable=True)             # para receber notificações
    senha_hash = Column(String, nullable=True)             # sha256 da senha do painel
    comissao_pct = Column(Integer, default=50)             # percentual (ex: 50 = 50%)
    ativo      = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())


class AffiliateConversion(Base):
    """Venda rastreada via link de afiliado."""
    __tablename__ = "affiliate_conversions"

    id             = Column(Integer, primary_key=True, index=True)
    affiliate_slug = Column(String, index=True)
    email_cliente  = Column(String, nullable=True)
    nome_cliente   = Column(String, nullable=True)
    whatsapp_cliente = Column(String, nullable=True)
    plano          = Column(String, nullable=True)
    valor          = Column(Integer, default=0)       # em centavos (ex: 19900)
    comissao       = Column(Integer, default=0)       # em centavos
    payment_id     = Column(String, nullable=True)    # ID do Mercado Pago
    metodo         = Column(String, nullable=True)    # pix | cartao
    created_at     = Column(DateTime, default=func.now())


class Garantia(Base):
    """Garantias de blindagem — uma linha por aparelho por usuário."""
    __tablename__ = "garantias"

    id          = Column(Integer, primary_key=True)
    user_email  = Column(String, index=True)   # dono do registro
    device_id   = Column(String, index=True)   # IMEI / serial do aparelho
    data_inicio = Column(String)
    data_fim    = Column(String)
    prazo       = Column(Integer)
    updated_at  = Column(DateTime, default=func.now(), onupdate=func.now())
