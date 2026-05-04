import anthropic
from app.core.config import settings


class ClaudeHandler:
    """룰 엔진이 처리 못한 복잡한 요청만 Claude에 위임."""

    def __init__(self):
        self._client = None

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    async def analyze(self, transcript: str, business_context: str = "") -> dict:
        system = (
            "당신은 소상공인 가게의 전화 비서입니다. "
            "고객의 말에서 인텐트(reservation/inquiry/complaint/other)와 "
            "핵심 슬롯(날짜, 인원, 항목 등)을 JSON으로 추출하세요.\n"
            f"가게 정보: {business_context}"
        )
        message = self._get_client().messages.create(
            model=settings.ai_model,
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": transcript}],
        )
        import json, re
        raw = message.content[0].text
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        return json.loads(json_match.group()) if json_match else {"intent": "other", "raw": raw}
