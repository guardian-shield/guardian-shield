from sqlalchemy import Column, Integer, String, DateTime
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    password = Column(String)
    plan_type = Column(String)
    expires_at = Column(DateTime, nullable=True)
    hwid_1 = Column(String)
    hwid_2 = Column(String)