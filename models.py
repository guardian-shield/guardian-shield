from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text
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
