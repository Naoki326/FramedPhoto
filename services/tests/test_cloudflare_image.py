"""Cloudflare contract; all credentials and HTTP responses are synthetic."""
import base64
import io
import json
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient
from PIL import Image

from app import llm, runtime_config, free_module
from app.config import settings
from app.main import app

client = TestClient(app)
ACCOUNT = "ab" * 16
TOKEN = "fake-cloudflare-token-123456"


def configure():
    return client.put('/api/ai', json={'image_api': 'cloudflare',
        'cloudflare_account_id': ACCOUNT, 'cloudflare_api_token': TOKEN})


def test_config_secret_lifecycle(monkeypatch):
    monkeypatch.setattr(settings, 'cloudflare_api_token', 'fake-env-token')
    r = configure()
    assert r.status_code == 200
    assert TOKEN not in r.text
    c = client.get('/api/ai').json()['config']
    assert c['cloudflare_account_id'] == ACCOUNT
    assert c['cloudflare_api_token_set'] is True
    assert '…' in c['cloudflare_api_token'] and TOKEN not in str(c)
    for value in ('', None, c['cloudflare_api_token']):
        client.put('/api/ai', json={'cloudflare_api_token': value})
        assert runtime_config.get('cloudflare_api_token') == TOKEN
    client.put('/api/ai', json={'cloudflare_api_token': '-'})
    c = client.get('/api/ai').json()['config']
    assert c['cloudflare_api_token'] == ''
    assert c['cloudflare_api_token_set'] is False
    assert llm.image_readiness()['ready'] is False


@pytest.mark.parametrize('payload', [
    {'image_api': 'invalid'}, {'cloudflare_account_id': '../evil'},
    {'cloudflare_account_id': 'a' * 31}, {'cloudflare_account_id': 'g' * 32},
    {'cloudflare_account_id': 'a' * 32 + '/x'},
])
def test_invalid_config(payload):
    assert client.put('/api/ai', json=payload).status_code == 400
    assert runtime_config.load() == {}


def test_ready_and_non_consuming_check(monkeypatch):
    post = Mock(side_effect=AssertionError('must not generate'))
    monkeypatch.setattr(requests, 'post', post)
    assert configure().status_code == 200
    s = client.get('/api/ai/status').json()
    assert s['cloudflare_ready'] is True and s['image_ready'] is True
    body = client.post('/api/ai/test', json={'target': 'image'}).json()
    assert body['ok'] is True and '未实际生成' in body['detail']
    assert '默认尺寸' in body['detail']
    client.put('/api/ai', json={'image_api': 'openai'})
    s = client.get('/api/ai/status').json()
    assert s['cloudflare_ready'] is True and s['image_ready'] is False
    post.assert_not_called()


def image_bytes(fmt='PNG'):
    buf = io.BytesIO()
    Image.new('RGB', (32, 24)).save(buf, format=fmt)
    return buf.getvalue()


@pytest.mark.parametrize('fmt', ['PNG', 'JPEG'])
def test_request_and_decode(monkeypatch, fmt):
    from app import cloudflare_image
    configure()
    raw = image_bytes(fmt)
    response = Mock(status_code=200)
    response.iter_content.return_value = [json.dumps({'success': True, 'result': {'image': base64.b64encode(raw).decode()}}).encode()]
    post = Mock(return_value=response)
    monkeypatch.setattr(cloudflare_image.requests, 'post', post)
    assert cloudflare_image.generate_image('a cat') == raw
    args, kw = post.call_args
    assert args == (f'https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/run/@cf/black-forest-labs/flux-1-schnell',)
    assert kw['headers']['Authorization'] == f'Bearer {TOKEN}'
    assert kw['json'] == {'prompt': 'a cat', 'steps': 4}
    assert kw['timeout'] == (5, 60)
    assert kw['allow_redirects'] is False


@pytest.mark.parametrize('status,payload', [
    (429, {}), (402, {}), (400, {}), (500, {}),
    (200, {'success': False, 'result': {'image': 'abcd'}}),
    (200, {'result': {'image': 'not base64!'}}),
    (200, {'result': {'image': base64.b64encode(b'not image').decode()}}),
    (200, {}), (200, {'result': []}),
])
def test_failure(monkeypatch, caplog, status, payload):
    from app import cloudflare_image
    configure()
    response = Mock(status_code=status)
    response.iter_content.return_value = [json.dumps(payload).encode()]
    monkeypatch.setattr(cloudflare_image.requests, 'post', Mock(return_value=response))
    assert cloudflare_image.generate_image('a cat') is None
    assert TOKEN not in caplog.text and 'not base64!' not in caplog.text


def test_no_network_without_valid_account(monkeypatch):
    from app import cloudflare_image
    runtime_config.save({'cloudflare_account_id': '../evil', 'cloudflare_api_token': TOKEN})
    post = Mock(side_effect=AssertionError('no HTTP'))
    monkeypatch.setattr(cloudflare_image.requests, 'post', post)
    assert cloudflare_image.generate_image('cat') is None
    assert llm.image_readiness()['cloudflare_ready'] is False
    post.assert_not_called()


@pytest.mark.parametrize('kind', ['timeout', 'bad_json', 'oversize_body', 'oversize_image', 'oversize_dimensions', 'gif'])
def test_response_boundaries(monkeypatch, kind):
    from app import cloudflare_image
    configure()
    response = Mock(status_code=200)
    raw = image_bytes('GIF' if kind == 'gif' else 'PNG')
    if kind == 'oversize_dimensions':
        buf = io.BytesIO()
        Image.new('RGB', (4097, 1)).save(buf, format='PNG')
        raw = buf.getvalue()
    body = json.dumps({'result': {'image': base64.b64encode(raw).decode()}}).encode()
    if kind == 'bad_json':
        body = b'invalid json'
    if kind == 'oversize_body':
        monkeypatch.setattr(cloudflare_image, 'MAX_RESPONSE_BYTES', 8)
    if kind == 'oversize_image':
        monkeypatch.setattr(cloudflare_image, 'MAX_IMAGE_BYTES', 8)
    response.iter_content.return_value = [body]
    post = Mock(return_value=response)
    if kind == 'timeout':
        post.side_effect = requests.Timeout('fake secret must not appear in logs')
    monkeypatch.setattr(cloudflare_image.requests, 'post', post)
    assert cloudflare_image.generate_image('cat') is None
    if kind != 'timeout':
        response.close.assert_called_once()


def test_account_clear_and_incomplete_readiness():
    configure()
    client.put('/api/ai', json={'cloudflare_account_id': ''})
    s = client.get('/api/ai/status').json()
    assert s['cloudflare_ready'] is False and s['image_ready'] is False
    body = client.post('/api/ai/test', json={'target': 'image'}).json()
    assert body['ok'] is False and '未实际生成' in body['detail']


@pytest.mark.parametrize('result', [None, b'fake-image'])
def test_cloudflare_never_falls_back(monkeypatch, result):
    from app import cloudflare_image
    configure()
    generate = Mock(return_value=result)
    monkeypatch.setattr(cloudflare_image, 'generate_image', generate)
    monkeypatch.setattr(free_module, '_generate_cartoon', Mock(side_effect=AssertionError('paid fallback')))
    monkeypatch.setattr(free_module, '_generate_signed', Mock(side_effect=AssertionError('paid fallback')))
    assert free_module._generate_illustration('cat') == result
    generate.assert_called_once_with('cat')
