"""DB 테이블 초기 생성 스크립트."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.db.base import Base
from app.db.session import engine
import app.db.models.call_log  # noqa: F401 — 모델 임포트로 Base에 등록

Base.metadata.create_all(bind=engine)
print("DB tables created.")
