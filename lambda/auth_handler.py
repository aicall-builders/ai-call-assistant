import json
import os
import requests
import base64
import firebase_admin
from firebase_admin import credentials, auth

# Firebase 초기화
try:
    firebase_admin.get_app()
except:
    try:
        # Base64로 인코딩된 서비스 계정 JSON 디코딩
        firebase_key_base64 = os.environ.get('FIREBASE_SERVICE_ACCOUNT_BASE64', '')
        if firebase_key_base64:
            firebase_key_json = base64.b64decode(firebase_key_base64).decode('utf-8')
            firebase_cred = credentials.Certificate(json.loads(firebase_key_json))
            firebase_admin.initialize_app(firebase_cred)
    except Exception as e:
        print(f"Firebase 초기화 실패: {str(e)}")

CORS_HEADERS = {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS'
}

def response(status, body):
    return {
        'statusCode': status,
        'headers': CORS_HEADERS,
        'body': json.dumps(body, ensure_ascii=False, default=str)
    }

def kakao_login(event):
    try:
        body = json.loads(event.get('body', '{}'))
    except:
        body = {}
    
    kakao_token = (
    body.get('kakao_access_token') or
    body.get('access_token') or
    body.get('accessToken')
    )
    if not kakao_token:
        return response(400, {'message': 'kakao_access_token 필수'})
    
    try:
        kakao_res = requests.get(
            'https://kapi.kakao.com/v2/user/me',
            headers={'Authorization': f'Bearer {kakao_token}'},
            timeout=5
        )
        kakao_user = kakao_res.json()
    except Exception as e:
        return response(401, {'message': f'Kakao API 호출 실패: {str(e)}'})

    if kakao_res.status_code != 200:
        return response(401, {'message': 'Invalid Kakao token'})

    kakao_id = str(kakao_user.get('id', 'unknown'))
    nickname = kakao_user.get('kakao_account', {}).get('profile', {}).get('nickname', 'User')
    uid = f'kakao:{kakao_id}'

    # Firebase Custom Token 발급
    try:
        custom_token = auth.create_custom_token(uid)
        custom_token_str = custom_token.decode('utf-8') if isinstance(custom_token, bytes) else custom_token
    except Exception as e:
        return response(500, {'message': f'Firebase Custom Token 발급 실패: {str(e)}'})

    return response(200, {
        'success': True,
        'custom_token': custom_token_str,
        'uid': uid,
        'kakao_id': kakao_id,
        'nickname': nickname,
        'user': {
            'id': kakao_id,
            'nickname': nickname,
            'email': None,
            'profile_image': None
        }
    })

def lambda_handler(event, context):
    try:
        path = event.get('path', event.get('rawPath', ''))
        method = event.get('requestContext', {}).get('http', {}).get('method', event.get('httpMethod', ''))
        
        if method == 'OPTIONS':
            return {
                'statusCode': 200,
                'headers': {
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
                    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS,PATCH,DELETE'
                },
                'body': json.dumps({'message': 'OK'})
            }
        
        if '/auth/kakao' in path and method == 'POST':
            return kakao_login(event)
        
        return response(404, {'message': 'Not Found'})
    except Exception as e:
        return response(500, {'message': f'Error: {str(e)}'})
