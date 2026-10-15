"""Behavior checks using temporary queues; no image service calls."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'image_queue.py'
spec = importlib.util.spec_from_file_location('queue_core', ROOT / 'skills/codex-image-queue/scripts/image_queue.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def png(color=(100, 160, 220)):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00' + bytes(color))) + chunk(b'IEND', b''))


class QueueBehavior(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'queue'
        self.q = core.Queue(self.home)
        self.source = self.root / 'source.png'
        self.source.write_bytes(png())

    def tearDown(self):
        self.temp.cleanup()

    def enqueue(self, name='job-1', output=None):
        return self.q.enqueue({'job_id': name, 'project': 'demo', 'items': [{
            'id': name, 'prompt': 'A painted potion bottle',
            'output_path': output or f'out/{name}.png'}]}, self.root)

    def cli(self, *args):
        r = subprocess.run([sys.executable, '-X', 'utf8', str(CLI), '--home', str(self.home), *args],
                           capture_output=True, text=True, encoding='utf-8', timeout=40)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def test_separate_processes_cannot_double_claim_or_exceed_limit(self):
        self.q.config(max_active=4)
        for i in range(12):
            self.enqueue(f'job-{i}')
        with ThreadPoolExecutor(max_workers=12) as pool:
            replies = list(pool.map(lambda i: self.cli('claim', '--worker-id', f'worker-{i}'), range(12)))
        claimed = [r for r in replies if r['claimed']]
        self.assertEqual(len(claimed), 4)
        self.assertEqual(len({r['job_id'] for r in claimed}), 4)
        self.assertEqual(len({r['claim_id'] for r in claimed}), 4)
        self.assertEqual(self.q.status()['counts'], {'in_progress': 4, 'queued': 8})
        self.assertFalse(self.q.claim(claimed[0]['worker_id'])['claimed'])

    def test_number_and_path_stay_reserved_after_completion(self):
        self.enqueue()
        claim = self.q.claim('worker-1')
        result = self.q.complete(claim['claim_id'], self.source)
        self.assertEqual(Path(result['output_path']).read_bytes(), self.source.read_bytes())
        with self.assertRaisesRegex(ValueError, 'job_id already exists'):
            self.enqueue()
        Path(result['output_path']).unlink()
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            self.enqueue('job-2', 'out/job-1.png')
        with self.assertRaisesRegex(ValueError, 'changed or disappeared'):
            self.q.complete(claim['claim_id'])

    def test_output_conflict_preserves_original(self):
        job = self.enqueue()
        claim = self.q.claim('worker-1')
        final = Path(job['output_path'])
        final.parent.mkdir()
        original = png((210, 50, 30))
        final.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'Output conflict'):
            self.q.complete(claim['claim_id'], self.source)
        self.assertEqual(final.read_bytes(), original)
        self.assertEqual(self.q.status()['jobs'][0]['status'], 'in_progress')

    def test_missing_and_nonimage_output_cannot_complete(self):
        self.enqueue()
        claim = self.q.claim('worker-1')
        with self.assertRaises(ValueError):
            self.q.complete(claim['claim_id'])
        text = self.root / 'error.txt'
        text.write_text('An upstream error, not an image.', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.q.complete(claim['claim_id'], text)
        self.assertEqual(self.q.status()['counts'], {'in_progress': 1})

    def test_recovery_is_preview_until_confirmed_and_revokes_old_claim(self):
        self.enqueue()
        old = self.q.claim('worker-old')
        preview = self.q.recover(old['claim_id'])
        self.assertTrue(preview['dry_run'])
        self.assertFalse(self.q.claim('worker-old')['claimed'])
        self.q.recover(old['claim_id'], confirmed=True)
        fresh = self.q.claim('worker-new')
        self.assertNotEqual(fresh['claim_id'], old['claim_id'])
        with self.assertRaisesRegex(ValueError, 'revoked'):
            self.q.complete(old['claim_id'], self.source)
        with self.assertRaises(ValueError):
            self.q.heartbeat(old['claim_id'])
        self.assertEqual(self.q.complete(fresh['claim_id'], self.source)['status'], 'completed')

    def test_recovery_adopts_saved_candidate_and_complete_is_idempotent(self):
        self.enqueue()
        claim = self.q.claim('worker-1')
        Path(claim['generation_output_path']).write_bytes(self.source.read_bytes())
        result = self.q.recover(claim['claim_id'], confirmed=True)
        self.assertEqual(Path(result['output_path']).read_bytes(), self.source.read_bytes())
        self.assertTrue(self.q.complete(claim['claim_id'])['already_completed'])

    def test_recover_publication_after_crash_before_db_commit(self):
        job = self.enqueue()
        claim = self.q.claim('worker-1')
        Path(claim['generation_output_path']).write_bytes(self.source.read_bytes())
        final = Path(job['output_path'])
        final.parent.mkdir()
        final.write_bytes(self.source.read_bytes())
        self.assertEqual(self.q.recover(claim['claim_id'], confirmed=True)['status'], 'completed')

    def test_failure_retry_and_recovery_have_total_attempt_limit(self):
        self.q.config(max_attempts=2)
        self.enqueue()
        first = self.q.claim('worker-1')
        self.q.fail(first['claim_id'], 'tool unavailable')
        self.assertEqual(self.q.retry('job-1')['status'], 'queued')
        second = self.q.claim('worker-2')
        self.assertEqual(second['attempt'], 2)
        self.assertEqual(self.q.recover(second['claim_id'], confirmed=True)['status'], 'failed')
        self.assertFalse(self.q.claim('worker-3')['claimed'])
        self.assertEqual(self.q.retry('job-1')['reason'], 'attempt limit reached')

    def test_reducing_attempt_limit_applies_to_waiting_jobs(self):
        self.enqueue()
        first = self.q.claim('worker-1')
        self.q.fail(first['claim_id'], 'temporary error')
        self.q.retry('job-1')
        self.q.config(max_attempts=1)
        self.assertFalse(self.q.claim('worker-2')['claimed'])
        self.assertEqual(self.q.status()['counts'], {'failed': 1})

    def test_stale_does_not_requeue_running_work(self):
        self.enqueue()
        first = self.q.claim('worker-1')
        self.assertTrue(self.q.status(stale_seconds=0)['jobs'][0]['stale'])
        self.assertFalse(self.q.claim('worker-2')['claimed'])
        self.assertTrue(self.q.heartbeat(first['claim_id'])['alive'])

    def test_json_relative_paths_and_bounded_wait(self):
        job = self.root / 'job.json'
        job.write_text(json.dumps({'job_id':'from-json', 'project':'演示', 'items':[{
            'id':'icon', 'prompt':'蓝色药瓶', 'output_path':'out/icon.png',
            'reference_images':['source.png']}]}), encoding='utf-8-sig')
        self.cli('enqueue', '--job-file', str(job), '--base-dir', str(self.root))
        claim = self.cli('claim', '--worker-id', 'worker-cli')
        self.assertEqual(Path(claim['next_item']['reference_images'][0]), self.source)
        self.assertTrue(self.cli('wait', '--job-id', 'from-json', '--timeout', '0')['timed_out'])
        self.cli('complete', '--claim-id', claim['claim_id'], '--source', str(self.source))
        self.assertEqual(self.cli('wait', '--job-id', 'from-json', '--timeout', '0')['counts'], {'completed':1})

    def test_installed_skill_runs_without_repository_and_refuses_overwrite(self):
        dest = self.root / 'installed'
        cmd = [sys.executable, '-X', 'utf8', str(ROOT / 'scripts/install_skill.py'), '--dest', str(dest)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        installed = dest / 'codex-image-queue'
        r = subprocess.run([sys.executable, str(installed / 'scripts/image_queue.py'), '--version'],
                           cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), core.VERSION)
        marker = installed / 'keep.txt'
        marker.write_text('keep', encoding='utf-8')
        self.assertNotEqual(subprocess.run(cmd, capture_output=True, timeout=30).returncode, 0)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')


if __name__ == '__main__':
    unittest.main()
