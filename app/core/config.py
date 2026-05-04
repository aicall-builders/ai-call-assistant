from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "AI Call Assistant"
    debug: bool = False

    # DB
    database_url: str = "sqlite:///./call_assistant.db"

    # STT
    stt_provider: str = "whisper"          # whisper | clova | google
    stt_model: str = "base"

    # AI
    ai_provider: str = "anthropic"
    ai_model: str = "claude-sonnet-4-6"
    anthropic_api_key: str = ""

    # Keyword engine
    keyword_confidence_threshold: float = 0.7
    ai_fallback_threshold: float = 0.4     # 룰 매칭 점수 이하면 AI 호출

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
