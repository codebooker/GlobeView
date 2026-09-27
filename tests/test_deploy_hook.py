import hashlib
import hmac
import json
import time
import unittest
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

from deploy import deploy_hook


class DeployHookTests(unittest.TestCase):
    def test_signature_is_bound_to_body_and_recent_timestamp(self):
        secret = b's' * 32
        timestamp = str(int(time.time()))
        body = b'{"release":1}'
        signature = hmac.new(secret, timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()

        self.assertTrue(deploy_hook.valid_signature(body, timestamp, signature, secret))
        self.assertFalse(deploy_hook.valid_signature(body + b' ', timestamp, signature, secret))
        self.assertFalse(deploy_hook.valid_signature(body, timestamp, signature, b'short'))

    def test_signature_expires_after_five_minutes(self):
        secret = b's' * 32
        timestamp = '1000000000'
        body = b'{}'
        signature = hmac.new(secret, timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()

        self.assertFalse(deploy_hook.valid_signature(body, timestamp, signature, secret, now=1000000301))

    def test_accepts_only_the_main_branch_of_this_repository(self):
        event = {
            'repository': 'codebooker/GlobeView',
            'ref': 'refs/heads/main',
            'sha': 'a' * 40,
        }
        self.assertEqual(deploy_hook.parse_deploy_target(json.dumps(event).encode()), event)

        for key, value in [('repository', 'fork/GlobeView'), ('ref', 'refs/heads/feature'), ('sha', 'z' * 40)]:
            invalid = dict(event)
            invalid[key] = value
            self.assertIsNone(deploy_hook.parse_deploy_target(json.dumps(invalid).encode()))

    def test_rejects_extra_fields_and_invalid_json(self):
        event = {
            'repository': 'codebooker/GlobeView',
            'ref': 'refs/heads/main',
            'sha': 'a' * 40,
            'command': 'anything',
        }
        self.assertIsNone(deploy_hook.parse_deploy_target(json.dumps(event).encode()))
        self.assertIsNone(deploy_hook.parse_deploy_target(b'{'))

    def test_http_endpoint_requires_signature_and_runs_only_verified_main_sha(self):
        class QuietHandler(deploy_hook.Handler):
            def log_message(self, fmt, *args):
                pass

        secret = b't' * 32
        server = deploy_hook.Server(('127.0.0.1', 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        deploy_hook.DEPLOY_SECRET = secret
        deploy_hook.REPLAY_CACHE.clear()
        thread.start()
        try:
            event = {
                'repository': 'codebooker/GlobeView',
                'ref': 'refs/heads/main',
                'sha': 'b' * 40,
            }
            body = json.dumps(event, separators=(',', ':'), sort_keys=True).encode()
            timestamp = str(int(time.time()))
            signature = hmac.new(secret, timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
            headers = {
                'Content-Type': 'application/json',
                'X-GlobeView-Timestamp': timestamp,
                'X-GlobeView-Signature': signature,
            }
            url = f'http://127.0.0.1:{server.server_port}/__deploy'

            with mock.patch.object(deploy_hook.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stderr='', stdout='')) as run:
                request = urllib.request.Request(url, data=body, headers=headers, method='POST')
                with urllib.request.urlopen(request, timeout=3) as response:
                    result = json.load(response)
                self.assertEqual(result, {'status': 'deployed', 'sha': event['sha']})
                run.assert_called_once_with(
                    [deploy_hook.DEPLOY_SCRIPT, event['sha']],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=12 * 60,
                )

            request = urllib.request.Request(url, data=body, headers=headers, method='POST')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 409)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            deploy_hook.DEPLOY_SECRET = b''
            deploy_hook.REPLAY_CACHE.clear()


if __name__ == '__main__':
    unittest.main()
