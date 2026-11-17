"""Check the Windows local demo; optional warmup only loads local models."""
import argparse
import json
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from app.core.config import PROJECT_ROOT, settings
from app.db.session import engine
from sqlalchemy import inspect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--warm-models', action='store_true')
    args = parser.parse_args()
    base = 'http://127.0.0.1:8000'
    def call(path, data=None, token=None, timeout=30):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                          headers=headers)
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    health = call('/api/health')
    assert all(component['ok'] for component in health['components']), 'Dependency health check failed'
    with urlopen('http://127.0.0.1:5173', timeout=10) as response:
        assert response.status == 200
    with urlopen(base + '/openapi.json', timeout=10) as response:
        paths = json.load(response)['paths']
    revision_api = '/api/admin/kb/documents/{doc_id}/revisions'
    assert revision_api in paths
    assert 'kb_chunk_revision' in inspect(engine).get_table_names()
    models = None
    local_search = None
    if args.warm_models:
        # Login and token stay in memory, never included in the report or logs.
        auth = call('/api/auth/login', {'username': settings.auth.admin_username,
                                       'password': settings.auth.admin_password})
        started = time.perf_counter()
        warmed = call('/api/models/load', {}, token=auth['access_token'], timeout=240)
        assert warmed['success']
        models = {'status': 'loaded', 'elapsed_seconds': round(time.perf_counter() - started, 2)}
        started = time.perf_counter()
        result = call('/api/search', {'query': '结直肠手术术前营养筛查', 'recall_mode': 'hybrid',
                                     'use_graph': False, 'use_rerank': True, 'top_k': 5}, timeout=120)
        assert result['results'], 'No local retrieval results'
        local_search = {'result_count': len(result['results']),
                        'elapsed_ms': round((time.perf_counter() - started) * 1000, 1),
                        'external_generation_calls': 0}
    report = {'verified_at': datetime.now(timezone.utc).isoformat(), 'timezone': 'Asia/Shanghai',
              'target': 'Windows localhost demo', 'status': 'passed', 'frontend_http_status': 200,
              'components': [{'name': component['name'], 'ok': component['ok']}
                             for component in health['components']],
              'revision_api_registered': True, 'revision_table_present': True,
              'stats': call('/api/stats'), 'models': models, 'local_search': local_search,
              'external_generation_calls': 0, 'credentials_recorded': False,
              'limitations': ['Fresh-machine installation is not verified.',
                             'Cloud answer/image generation is not called by this check.',
                             'Clinical review and independent labels remain pending.']}
    destination = PROJECT_ROOT / 'data/eval/local_demo_verification_20261105.json'
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
