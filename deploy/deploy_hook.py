#!/usr/bin/env python3
"""Authenticated, loopback-only deployment endpoint for GitHub Actions."""

import hashlib
import hmac
import http.server
import json
import os
import re
import subprocess
import threading
import time
from collections import OrderedDict


HOST = '127.0.0.1'
PORT = 8766
MAX_BODY_BYTES = 4096
MAX_CLOCK_SKEW_SECONDS = 300
MAX_REPLAY_ENTRIES = 2048
REPLAY_CACHE = OrderedDict()
REPLAY_LOCK = threading.Lock()
DEPLOY_LOCK = threading.Lock()
REQUEST_SLOTS = threading.BoundedSemaphore(8)
DEPLOY_SCRIPT = '/usr/local/sbin/globeview-release'
EXPECTED_REPOSITORY = 'codebooker/GlobeView'
DEPLOY_SECRET = os.environ.get('DEPLOY_WEBHOOK_SECRET', '').encode('utf-8')


def valid_signature(body, timestamp, signature, secret, now=None):
    if not re.fullmatch(r'[0-9]{10}', str(timestamp or '')):
        return False
    if not re.fullmatch(r'[0-9a-f]{64}', str(signature or '')) or len(secret) < 32:
        return False
    now = int(time.time()) if now is None else int(now)
    if abs(now - int(timestamp)) > MAX_CLOCK_SKEW_SECONDS:
        return False
    expected = hmac.new(secret, str(timestamp).encode() + b'.' + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def parse_deploy_target(body):
    try:
        event = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(event, dict) or set(event) != {'repository', 'ref', 'sha'}:
        return None
    if event.get('repository') != EXPECTED_REPOSITORY or event.get('ref') != 'refs/heads/main':
        return None
    if not isinstance(event.get('sha'), str) or not re.fullmatch(r'[0-9a-f]{40}', event['sha']):
        return None
    return event


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'GlobeViewDeployHook'
    sys_version = ''

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, fmt, *args):
        # Never write request headers or bodies (which include auth material).
        print('[deploy-hook] ' + (fmt % args), flush=True)

    def _reply(self, status, body):
        payload = json.dumps(body, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_GET(self):
        self._reply(405, {'error': 'method_not_allowed'})

    def do_POST(self):
        if self.path != '/__deploy':
            self._reply(404, {'error': 'not_found'})
            return
        if not DEPLOY_SECRET or len(DEPLOY_SECRET) < 32:
            self._reply(503, {'error': 'deployment_not_configured'})
            return
        if self.headers.get('Transfer-Encoding'):
            self._reply(400, {'error': 'invalid_request'})
            return
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            self._reply(411, {'error': 'content_length_required'})
            return
        if not 1 <= length <= MAX_BODY_BYTES:
            self._reply(413, {'error': 'request_too_large'})
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._reply(400, {'error': 'incomplete_request'})
            return

        timestamp = self.headers.get('X-GlobeView-Timestamp', '')
        signature = self.headers.get('X-GlobeView-Signature', '')
        now = int(time.time())
        if not valid_signature(body, timestamp, signature, DEPLOY_SECRET, now=now):
            try:
                stale = bool(re.fullmatch(r'[0-9]{10}', timestamp)) and abs(now - int(timestamp)) > MAX_CLOCK_SKEW_SECONDS
            except ValueError:
                stale = False
            self._reply(401, {'error': 'expired_signature' if stale else 'invalid_signature'})
            return

        replay_key = (timestamp, signature)
        with REPLAY_LOCK:
            while REPLAY_CACHE and now - REPLAY_CACHE[next(iter(REPLAY_CACHE))] > MAX_CLOCK_SKEW_SECONDS:
                REPLAY_CACHE.popitem(last=False)
            if replay_key in REPLAY_CACHE:
                self._reply(409, {'error': 'request_already_used'})
                return
            REPLAY_CACHE[replay_key] = now
            while len(REPLAY_CACHE) > MAX_REPLAY_ENTRIES:
                REPLAY_CACHE.popitem(last=False)

        event = parse_deploy_target(body)
        if event is None:
            self._reply(400, {'error': 'invalid_deployment_target'})
            return

        if not DEPLOY_LOCK.acquire(blocking=False):
            self._reply(409, {'error': 'deployment_in_progress'})
            return
        try:
            result = subprocess.run(
                [DEPLOY_SCRIPT, event['sha']],
                check=False,
                capture_output=True,
                text=True,
                timeout=12 * 60,
            )
        except subprocess.TimeoutExpired:
            print('[deploy-hook] deployment timed out', flush=True)
            self._reply(504, {'error': 'deployment_timed_out'})
            return
        finally:
            DEPLOY_LOCK.release()

        if result.returncode:
            detail = (result.stderr or result.stdout or '').strip()[-1000:]
            print(f"[deploy-hook] deploy failed for {event['sha']}: {detail}", flush=True)
            self._reply(500, {'error': 'deployment_failed'})
            return
        print(f"[deploy-hook] deployed {event['sha']}", flush=True)
        self._reply(200, {'status': 'deployed', 'sha': event['sha']})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8
    allow_reuse_address = False

    def process_request(self, request, client_address):
        if not REQUEST_SLOTS.acquire(blocking=False):
            try:
                request.sendall(
                    b'HTTP/1.1 503 Service Unavailable\r\n'
                    b'Connection: close\r\nContent-Length: 0\r\n\r\n'
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            REQUEST_SLOTS.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            REQUEST_SLOTS.release()


if __name__ == '__main__':
    Server((HOST, PORT), Handler).serve_forever(poll_interval=0.5)
