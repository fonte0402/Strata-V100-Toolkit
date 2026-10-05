"""Offline tests for collector runtime-manifest evidence validation."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import collect


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RuntimeManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.exe = self.root / 'server.exe'
        self.exe.write_bytes(b'fake server executable')
        self.config = self.root / 'config.json'
        # BOM exercises Windows PowerShell UTF-8 output compatibility.
        self.config.write_bytes(b'\xef\xbb\xbf' + json.dumps({
            'exe': str(self.exe), 'args': ['--ctx-size', '8192'],
            'api_key': 'must-not-appear',
        }).encode())
        self.manifest = self.root / 'manifest.json'
        self._write_manifest()

    def tearDown(self):
        self.temp.cleanup()

    def _write_manifest(self, **overrides):
        data = {
            'status': 'ready', 'port': 18099,
            'config_path': str(self.config), 'config_sha256': digest(self.config),
            'exe_sha256': digest(self.exe),
            'run_id': 'offline-test', 'args': ['--port', '18099'],
        }
        data.update(overrides)
        self.manifest.write_text(json.dumps(data), encoding='utf-8')

    def test_missing_manifest_rejected(self):
        with self.assertRaises(FileNotFoundError):
            collect.load_runtime_manifest(self.root / 'missing.json', 'http://127.0.0.1:18099')

    def test_non_ready_manifest_rejected(self):
        self._write_manifest(status='exited')
        with self.assertRaisesRegex(ValueError, 'status must be ready'):
            collect.load_runtime_manifest(self.manifest, 'http://127.0.0.1:18099')

    def test_port_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, 'does not match'):
            collect.load_runtime_manifest(self.manifest, 'http://127.0.0.1:18100')

    def test_config_hash_change_rejected(self):
        self.config.write_text('{"exe":"changed"}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'config file SHA-256 changed'):
            collect.load_runtime_manifest(self.manifest, 'http://127.0.0.1:18099')

    def test_executable_hash_change_rejected(self):
        self.exe.write_bytes(b'changed executable')
        with self.assertRaisesRegex(ValueError, 'executable SHA-256 changed'):
            collect.load_runtime_manifest(self.manifest, 'http://127.0.0.1:18099')

    def test_valid_manifest_snapshots_paths_hashes_without_verifying_live_conditions(self):
        evidence, config = collect.load_runtime_manifest(self.manifest, 'http://127.0.0.1:18099')
        self.assertEqual(evidence['status'], 'validated_ready_manifest')
        self.assertTrue(evidence['validation']['config_hash_matches'])
        self.assertEqual({item['role'] for item in evidence['files']},
                         {'runtime_manifest', 'service_config', 'server_executable'})
        self.assertEqual(config['exe_sha256'], digest(self.exe))
        self.assertNotIn('must-not-appear', json.dumps([evidence, config]))
        # This feature is evidence capture only, never live-process attestation.
        self.assertIn('does not independently attest the live process', evidence['note'])

    def test_manifest_without_config_uses_direct_launch_record(self):
        self._write_manifest(config_path=None, config_sha256=None,
                             exe_path=str(self.exe))
        evidence, config = collect.load_runtime_manifest(self.manifest, 'http://127.0.0.1:18099')
        self.assertIsNone(evidence['validation']['config_hash_matches'])
        self.assertEqual(config['source'], 'manifest_launch_record')


if __name__ == '__main__':
    unittest.main()
