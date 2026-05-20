import json
import uuid
import boto3
import pymysql
import requests

# ── 환경변수 ──────────────────────────────────────────────
import os
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DB_SECRET_ARN     = 'arn:aws:secretsmanager:ap-northeast-2:135775632268:secret:rds!db-aefb8895-1f09-4168-a4e4-45b4b9a1b076-94BMFa'
DB_HOST           = 'call-recorder-db.czem0u8m8xfi.ap-northeast-2.rds.amazonaws.com'
DB_NAME           = 'call_recorder'

# ── AWS 클라이언트 ────────────────────────────────────────
secrets_client = boto3.client('secretsmanager', region_name='ap-northeast-2')

# ── DB 연결 ───────────────────────────────────────────────
def get_db():
    secret = secrets_client.get_secret_value(SecretId=DB_SECRET_ARN)
    creds  = json.loads(secret['SecretString'])
    return pymysql.connect(
        host=DB_HOST,
        user=creds['username'],
        password=creds['password'],
        db=DB_NAME,
        port=3306,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        ssl_disabled=True
    )

# ── 공통 응답 ─────────────────────────────────────────────
CORS_HEADERS = {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS,PATCH,DELETE'
}

def response(status, body):
    return {
        'statusCode': status,
        'headers': CORS_HEADERS,
        'body': json.dumps(body, ensure_ascii=False, default=str)
    }

# ── keywords.json 로드 ────────────────────────────────────
KEYWORDS = None
def get_keywords():
    global KEYWORDS
    if KEYWORDS is None:
        with open('keywords.json', 'r', encoding='utf-8') as f:
            KEYWORDS = json.load(f)
    return KEYWORDS

def match_keywords(text, industry='food'):
    kw_data    = get_keywords()
    categories = kw_data['industries'].get(industry, {}).get('categories', {})
    matched    = []
    matched_category = None
    highest_priority = 999

    for cat_key, cat_val in categories.items():
        for kw in cat_val.get('keywords', []):
            if kw in text:
                matched.append(kw)
                if cat_val['priority'] < highest_priority:
                    highest_priority = cat_val['priority']
                    matched_category = cat_key

    for kw in kw_data['global_keywords']['urgent']['keywords']:
        if kw in text:
            matched.append(kw)
            matched_category = 'urgent'

    return list(set(matched)), matched_category


# ══════════════════════════════════════════════════════════
# STT 정규화 (1차 LLM)
# ══════════════════════════════════════════════════════════
def normalize_stt_with_llm(stt_text, call_created_at=None):
    if not ANTHROPIC_API_KEY:
        print("[NORMALIZE] API 키 없음 - 정규화 스킵")
        return stt_text

    from datetime import datetime
    if call_created_at:
        if isinstance(call_created_at, str):
            base_time = call_created_at
        else:
            base_time = call_created_at.strftime("%Y-%m-%d %H:%M (%A)")
    else:
        base_time = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")

    prompt = f"""다음은 통화 녹음의 STT 변환 텍스트입니다. 이 텍스트를 정규화해주세요.

[통화 발생 시점]
{base_time}

[원본 STT 텍스트]
{stt_text}

[정규화 규칙]
1. 필러 제거: "음", "어", "아", "그", "저" 같은 의미 없는 추임새 제거
2. 문맥/문법 보정: 한글 문맥상 부자연스러운 표현 자연스럽게 수정 (단, 의미는 보존)
3. 시간 표현 변환: "내일", "오늘", "다음주 월요일" 같은 상대적 시간 표현을 실제 날짜(YYYY-MM-DD)로 변환
4. 시각 변환: "저녁 7시" → "19:00" 등 24시간제로
5. 전화번호 포맷: "공일공 일이삼사 오육칠팔" → "010-1234-5678"
6. 화자 라벨 변경: "[화자1]" → "발신자:", "[화자2]" → "수신자:"
7. 비언어 표시: 한숨, 웃음, 침묵 등은 [한숨], [웃음], [침묵] 등으로 표시
8. 비속어: 그대로 유지 (마스킹 X)

[중요]
- 원본 의미를 절대 왜곡하지 마세요
- 정규화된 텍스트만 응답하세요 (설명, 주석, 마크다운 없이)

[정규화된 텍스트]"""

    try:
        res = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {ANTHROPIC_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            },
            timeout=30
        )
        res.raise_for_status()
        data = res.json()
        normalized = data['choices'][0]['message']['content'].strip()

        if '```' in normalized:
            parts = normalized.split('```')
            if len(parts) >= 2:
                normalized = parts[1]
                if normalized.startswith('text\n'):
                    normalized = normalized[5:]
                normalized = normalized.strip()

        print(f"[NORMALIZE] 정규화 완료. 길이: {len(stt_text)} → {len(normalized)}")
        return normalized

    except Exception as e:
        print(f"[NORMALIZE ERROR] {str(e)}")
        return stt_text


# ══════════════════════════════════════════════════════════
# LLM 요약 (구조화 정보 추출)
# ══════════════════════════════════════════════════════════
def summarize_with_llm(stt_text, caller_number=None, duration_sec=None, store_name=None):
    if not ANTHROPIC_API_KEY:
        print("[LLM] API 키 없음 - 스킵")
        return None

    context = ""
    if store_name:
        context += f"가게명: {store_name}\n"
    if duration_sec:
        minutes = duration_sec // 60
        seconds = duration_sec % 60
        context += f"통화 길이: {minutes}분 {seconds}초\n"
    if caller_number:
        context += f"발신 번호: {caller_number}\n"

    prompt = f"""다음은 소상공인 식당에 걸려온 통화 녹음의 STT 변환 텍스트입니다.

{context}
[통화 내용]
{stt_text}

위 통화를 분석해서 아래 JSON 형식으로만 응답해주세요. JSON 외 다른 텍스트는 절대 포함하지 마세요.

{{
  "summary": "통화 내용을 3줄 이내로 요약",
  "category": "예약/주문/취소/환불/불만/문의/칭찬/기타 중 하나",
  "sentiment": "positive/neutral/negative 중 하나",
  "action_required": true 또는 false,
  "keywords": ["핵심 키워드1", "핵심 키워드2", "핵심 키워드3"],
  "extracted_info": {{
    "category_code": "reservation/order/cancel_refund/complaint/hours_location/price/ingredients_allergy/catering_bulk/positive/other 중 하나",
    "customer_name": "고객 이름 (없으면 null)",
    "phone": "고객 전화번호 (없으면 null, 형식: 010-1234-5678)",
    "date": "예약/주문 날짜 (YYYY-MM-DD, 없으면 null)",
    "time": "예약/주문 시간 (HH:MM 24시간제, 없으면 null)",
    "party_size": "예약 인원 수 (정수, 없으면 null)",
    "menu": ["주문/언급된 메뉴 배열, 없으면 빈 배열 []"],
    "special_notes": "특이사항/요청사항 한 문장 (없으면 null)"
  }}
}}

규칙:
- 통화 내용에 명시되지 않은 정보는 절대 추측하지 말고 null로 두세요.
- category_code는 위 영문 코드 중 하나를 정확히 사용하세요."""

    try:
        res = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {ANTHROPIC_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        res.raise_for_status()
        data = res.json()
        text = data['choices'][0]['message']['content'].strip()

        if '```' in text:
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]

        result = json.loads(text.strip())
        print(f"[LLM] 요약 완료: {result}")
        return result

    except Exception as e:
        print(f"[LLM ERROR] {str(e)}")
        return None


# ══════════════════════════════════════════════════════════
# 라우터
# ══════════════════════════════════════════════════════════
def lambda_handler(event, context):
    path   = event.get('path') or event.get('rawPath', '')
    method = event.get('httpMethod') or \
             event.get('requestContext', {}).get('http', {}).get('method', '')

    print(f"[NLP ROUTER] {method} {path}")

    if method == 'OPTIONS':
        return response(200, {'message': 'OK'})

    if '/clova/webhook' in path and method == 'POST':
        return clova_webhook(event)

    return response(404, {'message': 'Not Found'})


# ══════════════════════════════════════════════════════════
# CLOVA Webhook → LLM 요약 연동
# ══════════════════════════════════════════════════════════
def clova_webhook(event):
    body   = json.loads(event.get('body', '{}'))
    token  = body.get('token')
    result = body.get('result')

    print(f"[WEBHOOK FULL BODY] {json.dumps(body, ensure_ascii=False)[:3000]}")
    print(f"[WEBHOOK] token={token} result={result}")

    if not token:
        return response(400, {'message': 'token이 없습니다.'})

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT c.id, c.store_id, c.caller_number, c.caller_category, c.duration,
                       s.name as store_name
                FROM calls c
                JOIN stores s ON c.store_id = s.id
                WHERE c.clova_job_id = %s
            """, (token,))
            call = cursor.fetchone()

            if not call:
                return response(404, {'message': 'call을 찾을 수 없습니다.'})

            call_id    = call['id']
            store_name = call['store_name']

            if result == 'SUCCEEDED':
                segments = body.get('segments', [])
                raw_text = '\n'.join([
                    f"[화자{seg.get('speaker', {}).get('label', '?')}]: {seg.get('text', '').strip()}"
                    for seg in segments if seg.get('text', '').strip()
                ]) or body.get('text', '')

                cursor.execute("SELECT created_at FROM calls WHERE id = %s", (call_id,))
                call_info = cursor.fetchone()
                call_created_at = call_info['created_at'] if call_info else None

                full_text = normalize_stt_with_llm(raw_text, call_created_at)

                print(f"[WEBHOOK] 정규화 전: {raw_text[:200]}")
                print(f"[WEBHOOK] 정규화 후: {full_text[:200]}")

                matched_kws, matched_cat = match_keywords(full_text)

                cursor.execute("""
                    UPDATE calls SET status = 'transcribed', stt_result = %s
                    WHERE id = %s
                """, (full_text, call_id))

                llm_result = summarize_with_llm(
                    stt_text=full_text,
                    caller_number=call['caller_number'],
                    duration_sec=call['duration'],
                    store_name=store_name
                )

                summary_id      = str(uuid.uuid4())
                summary_text    = llm_result.get('summary') if llm_result else None
                category        = llm_result.get('category', matched_cat) if llm_result else matched_cat
                sentiment       = llm_result.get('sentiment') if llm_result else None
                action_required = llm_result.get('action_required', False) if llm_result else False
                llm_keywords    = llm_result.get('keywords', []) if llm_result else []
                all_keywords    = list(set(matched_kws + llm_keywords))
                extracted_info  = llm_result.get('extracted_info') if llm_result else None

                cursor.execute("""
                    INSERT INTO summaries
                        (id, call_id, summary, category, sentiment, action_required, keywords, extracted_info)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        summary = VALUES(summary), category = VALUES(category),
                        sentiment = VALUES(sentiment), action_required = VALUES(action_required),
                        keywords = VALUES(keywords), extracted_info = VALUES(extracted_info)
                """, (
                    summary_id, call_id, summary_text, category,
                    sentiment, 1 if action_required else 0,
                    json.dumps(all_keywords, ensure_ascii=False),
                    json.dumps(extracted_info, ensure_ascii=False) if extracted_info else None
                ))

                cursor.execute("UPDATE calls SET status = 'summarized' WHERE id = %s", (call_id,))
                print(f"[SUCCESS] call_id={call_id} 키워드={all_keywords}")

            else:
                error_msg = body.get('message', f'CLOVA STT 실패 (result={result})')
                cursor.execute("""
                    UPDATE calls SET status = 'error', error_message = %s WHERE id = %s
                """, (error_msg, call_id))

        conn.commit()
    finally:
        conn.close()

    return response(200, {'success': True, 'call_id': call_id})