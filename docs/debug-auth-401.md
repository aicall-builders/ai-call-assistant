# 로그인 401 디버깅 메모

로그 확인 결과 auth upsert는 성공한다.

- `[Auth] DB user upsert ok firebase_uid=kakao:4928396669`
- `[Auth] firebase init ok uid=kakao:4928396669`
- 이후 `/stores` 호출에서 401 발생 추정

1차 원인 후보:

- 프론트가 `/stores` 호출 시 `Authorization: Bearer <Firebase ID Token>`을 안 붙임
- 또는 `call-recorder-api-call` 패키지 내 `auth_handler.py`/Firebase Admin 설정이 `call-recorder-api-auth`와 불일치

확인 필요:

- Network `/stores` 요청 헤더의 Authorization 존재 여부
- `call-recorder-api-call` CloudWatch에서 `_get_current_user_id` 상세 로그
