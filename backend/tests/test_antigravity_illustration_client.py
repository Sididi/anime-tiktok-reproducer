"""Exercise the real subprocess boundary without signing in or spending quota."""
import base64
import hashlib
import json
import os
import sys
import time

import pytest

from app.config import settings
from app.services.antigravity_illustration_client import AntigravityIllustrationClient as Client


@pytest.fixture
def cli(tmp_path, monkeypatch):
    executable = tmp_path / 'fake agy'
    executable.write_text('#!' + sys.executable + '\n' + '''
import hashlib, json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
option = lambda name: args[args.index(name) + 1]
settings = json.loads(option('--settings'))
assert settings['useG1Credits'] is False and settings['modelProvider'] == ''
assert settings['permissions']['deny'] == ['command(*)','write_file(*)','read_url(*)','execute_url(*)','mcp(*)']
assert not any(k in os.environ for k in ('GEMINI_API_KEY','GOOGLE_API_KEY','GOOGLE_APPLICATION_CREDENTIALS','LD_LIBRARY_PATH'))
assert '--dangerously-skip-permissions' not in args
assert option('--input-format') == 'stream-json'
mode = os.environ.get('ATR_AGY_TEST_MODE', 'ok')
if mode == 'slow-input':
    time.sleep(.6)
content = json.loads(sys.stdin.readline())['message']['content']
assert content.endswith('Describe supplied evidence.')
assert sys.stdin.read() == ''
if mode == 'slow-input':
    prompt = content.split('\\n\\n', 1)[1]
    assert hashlib.sha256(prompt.encode()).hexdigest() == os.environ['ATR_AGY_TEST_PROMPT_SHA256']
assert json.loads(Path(option('--json-schema')).read_text())['type'] == 'object'
if mode == 'hang':
    Path(os.environ['ATR_AGY_TEST_PID']).write_text(str(os.getpid()))
    time.sleep(30)
if mode == 'broken':
    print('{unfinished')
    sys.exit(0)
if mode == 'quota':
    print(json.dumps({'event':'result','result':{'status':'ERROR','error':'Quota exhausted'}}))
    sys.exit(1)
for path in Path('.').glob('image-*'):
    if mode != 'unread':
        print(json.dumps({'event':'step_update','step_update':{'state':'DONE','tool_name':'read_file',
            'tool_info':{'parameters':{'Path':str(path.resolve())}}}}))
print(json.dumps({'event':'result','result':{'status':'SUCCESS','structured_output':{'ok':True},
    'conversation_id':'fixture','usage':{'input_tokens':10,'output_tokens':5}}}))
''')
    executable.chmod(0o755)
    monkeypatch.setattr(settings, 'antigravity_cli_binary', str(executable))
    monkeypatch.setenv('GEMINI_API_KEY', 'must-not-reach-cli')
    monkeypatch.setenv('GOOGLE_API_KEY', 'must-not-reach-cli')
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS', '/not/a/key')
    monkeypatch.setenv('LD_LIBRARY_PATH', '/not/a/library')
    monkeypatch.setenv('ATR_AGY_TEST_PID', str(tmp_path / 'child.pid'))
    return tmp_path


def request(root, check=lambda: None, timeout=3, prompt='Describe supplied evidence.'):
    return Client(root / 'calls', time.monotonic() + timeout, check).call('vision', prompt,
        {'type':'object'}, model='pinned-model', images=['data:image/png;base64,' + base64.b64encode(b'fixture').decode()])


def test_subscription_json_image_evidence_and_usage_are_recorded(cli):
    assert request(cli) == {'ok': True}
    assert json.loads((cli / 'calls/vision.response.json').read_text()) == {'ok': True}
    record = json.loads((cli / 'calls/vision.usage.json').read_text())['calls'][0]
    assert record['billing'] == 'subscription' and record['credit_overages'] is False
    assert record['image_count'] == record['images_read'] == 1
    assert record['usage']['input_tokens'] == 10 and record['model'] == 'pinned-model'


def test_slow_cli_receives_entire_large_unicode_prompt_and_eof(cli, monkeypatch):
    prompt = 'évidence 漢字 🧪\n' * 16_384 + 'Describe supplied evidence.'
    monkeypatch.setenv('ATR_AGY_TEST_MODE', 'slow-input')
    monkeypatch.setenv('ATR_AGY_TEST_PROMPT_SHA256', hashlib.sha256(prompt.encode()).hexdigest())
    checks = []
    assert request(cli, check=lambda: checks.append(time.monotonic()), prompt=prompt) == {'ok': True}
    assert len(checks) >= 3


@pytest.mark.parametrize('mode, message', [('quota','Quota exhausted'),('unread','toutes les images'),('broken','JSON complète')])
def test_failure_stops_without_fallback_or_hidden_retry(cli, monkeypatch, mode, message):
    monkeypatch.setenv('ATR_AGY_TEST_MODE', mode)
    with pytest.raises(ValueError, match=message):
        request(cli)
    calls = json.loads((cli / 'calls/vision.usage.json').read_text())['calls']
    assert len(calls) == 1 and calls[0]['status'] == 'error'


def test_global_deadline_terminates_cli_process(cli, monkeypatch):
    monkeypatch.setenv('ATR_AGY_TEST_MODE', 'hang')
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        request(cli, timeout=.4)
    assert time.monotonic() - started < 2
    pid = int((cli / 'child.pid').read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cancellation_terminates_inflight_request(cli, monkeypatch):
    monkeypatch.setenv('ATR_AGY_TEST_MODE', 'hang')
    def check():
        if (cli / 'child.pid').exists():
            raise RuntimeError('Canceled by user')
    with pytest.raises(RuntimeError, match='Canceled'):
        request(cli, check=check)
    with pytest.raises(ProcessLookupError):
        os.kill(int((cli / 'child.pid').read_text()), 0)
