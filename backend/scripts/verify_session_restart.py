"""Verify persisted history across an explicitly requested local API restart.

Only restarts the verified loopback uvicorn application on port 8000. The
ephemeral visitor cookie remains in memory and is never included in reports.
"""
import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, build_opener, HTTPCookieProcessor
from app.core.config import PROJECT_ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--restart',action='store_true',required=True)
    parser.parse_args()
    base='http://127.0.0.1:8000'
    client=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def call(path,data=None,method=None):
        request=Request(base+path,data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type':'application/json'},method=method)
        with client.open(request,timeout=30) as response:
            return json.load(response)
    for _ in range(30):
        try:
            call('/api/health/live'); break
        except Exception: time.sleep(1)
    else: raise RuntimeError('API not ready')
    result=call('/api/chat',{'question':'你是谁','use_intent_routing':False})
    sid=result['session_id']
    try:
        before=call('/api/chat/'+sid)
        output=subprocess.check_output(['netstat','-ano','-p','tcp'],text=True)
        match=re.search(r'^\s*TCP\s+127\.0\.0\.1:8000\s+\S+\s+LISTENING\s+(\d+)\s*$',output,re.M)
        if not match: raise RuntimeError('No verified loopback listener')
        pid=int(match.group(1))
        expected=str(Path(sys._base_executable)).replace("'","''")
        command=(f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
            f"if ($p.ExecutablePath -ne '{expected}' -or $p.CommandLine -notmatch '-m uvicorn app\\.main:app --host 127\\.0\\.0\\.1 --port 8000') "
            "{ throw 'Process identity mismatch; no process stopped' }; "
            f"Stop-Process -Id {pid} -Force -ErrorAction Stop")
        subprocess.run(['powershell','-NoProfile','-Command',command],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        environment=dict(os.environ,PYTHONPATH=str(PROJECT_ROOT/'backend'),HF_HUB_OFFLINE='1',PYTHONIOENCODING='utf-8')
        with (PROJECT_ROOT/'logs/backend-improvements.log').open('ab') as out, (PROJECT_ROOT/'logs/backend-improvements.err.log').open('ab') as err:
            subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000'],
                cwd=PROJECT_ROOT/'backend',stdout=out,stderr=err,env=environment,creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(60):
            try:
                call('/api/health/live'); break
            except Exception: time.sleep(1)
        else: raise RuntimeError('Restarted API did not become ready')
        after=call('/api/chat/'+sid)
        assert before['turns']==after['turns'] and after['turn_count']==1
        report={'status':'passed','history_restored_after_real_backend_restart':True,
            'turn_count':after['turn_count'],'cookie_or_token_recorded':False,
            'cases_public_total':call('/api/cases')['total'],
            'image_feature_model':call('/api/images/similar',{'image_id':'93ff859b-7220-43fa-9a20-58ef37b65f12','top_k':3}).get('feature_model')}
        (PROJECT_ROOT/'data/eval/live_restart_20261104.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False),flush=True)
    finally:
        call('/api/chat/'+sid,method='DELETE')


if __name__=='__main__':
    main()
