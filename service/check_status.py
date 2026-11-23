"""Read-only local service check. Never submits work or prints credentials."""
import importlib.util
import json
import pathlib
import sys
import urllib.request
import urllib.error

APP = pathlib.Path(__file__).resolve().parents[1]

def check():
    cfg = json.loads((APP/'config.json').read_text('utf-8'))
    packages = {name: importlib.util.find_spec(name) is not None
                for name in ('playwright','rfc8785','PIL','comtypes')}
    result = {'python':sys.executable,'dependencies':packages,
              'service_url':f"http://127.0.0.1:{int(cfg.get('port',8205))}",
              'release_scope':'local browser pilot; supervised desktop input implemented but live acceptance pending',
              'desktop_input':{'default_enabled':False,'entrypoint':'service/desktop_console.py','http_or_ai_enabled':False,'live_acceptance':'PENDING'},
              'feature_flags':{k:cfg.get(k,False) for k in ('vision_enabled','direct_api_enabled','uia_enabled','onevault_enabled')}}
    token = pathlib.Path.home()/'.browser-engine-desktop/token'
    if not token.is_file():
        result.update(status='NOT_INITIALIZED',reason='Local auth token not created yet')
        return result
    try:
        auth_token = token.read_text('utf-8').strip()
    except OSError:
        result.update(status='AUTH_TOKEN_UNREADABLE',reason='Run status check as the Windows user who owns the service token')
        return result
    try:
        request=urllib.request.Request(result['service_url']+'/api/status',headers={'X-Desktop-Token':auth_token})
        with urllib.request.urlopen(request,timeout=5) as response:
            state=json.load(response)
        result.update(status='RUNNING',model_ready=state.get('vllm_ready') is True,
                      active_task=state.get('active_task_id'),login_in_progress=state.get('login_in_progress'),
                      pending_approvals=state.get('pending_approvals'))
    except urllib.error.HTTPError as exc:
        result.update(status='HTTP_ERROR',http_status=exc.code)
    except Exception as exc:
        result.update(status='UNAVAILABLE',error_type=type(exc).__name__)
    return result

if __name__=='__main__':
    result=check();print(json.dumps(result,ensure_ascii=False,indent=2))
    sys.exit(0 if result.get('status')=='RUNNING' and result.get('model_ready') else 2)
