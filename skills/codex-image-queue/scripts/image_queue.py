#!/usr/bin/env python3
"""Portable JSON image jobs, coordinated by a local SQLite transaction ledger."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import uuid

VERSION = '0.1.0'
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z')


def identifier(value, field):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f'{field}: use 1–120 letters, digits, dots, hyphens or underscores')
    return value


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def absolute(path, base):
    p = Path(path).expanduser()
    return (p if p.is_absolute() else base / p).resolve()


def inspect_image(path):
    p = Path(path)
    if not p.is_file() or p.stat().st_size < 16:
        raise ValueError('Result must be a non-empty image file')
    with p.open('rb') as stream:
        head = stream.read(16)
    valid = (head.startswith(b'\x89PNG\r\n\x1a\n') or head.startswith(b'\xff\xd8\xff')
             or head.startswith((b'GIF87a', b'GIF89a'))
             or (head.startswith(b'RIFF') and head[8:12] == b'WEBP'))
    if not valid:
        raise ValueError('Expected PNG, JPEG, GIF or WebP header; visual QA remains separate')


class Queue:
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.db = self.home / 'queue.sqlite3'
        with self.connection() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT OR IGNORE INTO settings VALUES ('max_active',3),('max_attempts',3);
                CREATE TABLE IF NOT EXISTS jobs (
                  job_id TEXT PRIMARY KEY, project TEXT NOT NULL, item_id TEXT NOT NULL,
                  spec TEXT NOT NULL, output_path TEXT NOT NULL, output_key TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL, claim_id TEXT, worker_id TEXT, attempt INTEGER NOT NULL DEFAULT 0,
                  attempt_path TEXT, updated REAL NOT NULL, created REAL NOT NULL, error TEXT,
                  result_sha256 TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, job_id TEXT,
                  action TEXT NOT NULL, detail TEXT NOT NULL
                );
            ''')

    @contextmanager
    def connection(self, write=False):
        con = sqlite3.connect(self.db, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA synchronous=FULL')
            if write:
                con.execute('BEGIN IMMEDIATE')
            yield con
            if write:
                con.commit()
        except BaseException:
            if write:
                con.rollback()
            raise
        finally:
            con.close()

    def event(self, con, job_id, action, **detail):
        con.execute('INSERT INTO events(at,job_id,action,detail) VALUES(?,?,?,?)',
                    (time.time(), job_id, action, json.dumps(detail, ensure_ascii=False)))

    def config(self, max_active=None, max_attempts=None):
        with self.connection(True) as con:
            for k,v in [('max_active',max_active),('max_attempts',max_attempts)]:
                if v is not None:
                    if not 1 <= v <= 64:
                        raise ValueError(f'{k} must be between 1 and 64 (local setting, not provider quota)')
                    con.execute('UPDATE settings SET value=? WHERE key=?',(v,k))
            return dict(con.execute('SELECT key,value FROM settings').fetchall())

    def enqueue(self, spec, base):
        if not isinstance(spec,dict) or not isinstance(spec.get('items'),list) or len(spec['items'])!=1:
            raise ValueError('Use one JSON job with exactly one items entry per image')
        job_id = identifier(spec.get('job_id'),'job_id')
        item = spec['items'][0]
        if not isinstance(item,dict):
            raise ValueError('items[0] must be an object')
        item_id = identifier(item.get('id'),'items[0].id')
        if not isinstance(spec.get('project'),str) or not spec['project'].strip():
            raise ValueError('project is required')
        if not isinstance(item.get('prompt'),str) or not item['prompt'].strip():
            raise ValueError('items[0].prompt is required')
        if not isinstance(item.get('output_path'),str) or not item['output_path'].strip():
            raise ValueError('items[0].output_path is required')
        output = absolute(item['output_path'],Path(base).resolve())
        if output.is_relative_to(self.home):
            raise ValueError('Final output must be outside the queue state folder')
        if output.suffix.lower() not in {'.png','.jpg','.jpeg','.gif','.webp'}:
            raise ValueError('Choose a PNG, JPEG, GIF or WebP output filename')
        normalized = json.loads(json.dumps(spec))
        normalized['items'][0]['output_path'] = str(output)
        refs = normalized['items'][0].get('reference_images',[])
        if not isinstance(refs,list) or any(not isinstance(x,str) for x in refs):
            raise ValueError('reference_images must be an array of local image paths')
        normalized['items'][0]['reference_images'] = [str(absolute(x,Path(base).resolve())) for x in refs]
        with self.connection(True) as con:
            if con.execute('SELECT 1 FROM jobs WHERE job_id=?',(job_id,)).fetchone():
                raise ValueError('job_id already exists, including finished or failed jobs')
            if output.exists():
                raise ValueError('output_path already exists; use a new filename')
            key = os.path.normcase(str(output))
            if con.execute('SELECT 1 FROM jobs WHERE output_key=?',(key,)).fetchone():
                raise ValueError('output_path is already reserved by another job')
            now = time.time()
            con.execute('''INSERT INTO jobs(job_id,project,item_id,spec,output_path,output_key,status,updated,created)
                           VALUES(?,?,?,?,?,?,'queued',?,?)''',
                        (job_id,spec['project'],item_id,json.dumps(normalized,ensure_ascii=False),str(output),key,now,now))
            self.event(con,job_id,'enqueued')
        return {'job_id':job_id,'status':'queued','output_path':str(output)}

    def claim(self, worker_id):
        identifier(worker_id,'worker_id')
        with self.connection(True) as con:
            limit = con.execute("SELECT value FROM settings WHERE key='max_active'").fetchone()[0]
            attempts = con.execute("SELECT value FROM settings WHERE key='max_attempts'").fetchone()[0]
            exhausted = con.execute("SELECT * FROM jobs WHERE status='queued' AND attempt>=?",(attempts,)).fetchall()
            for job in exhausted:
                con.execute("UPDATE jobs SET status='failed',claim_id=NULL,error='attempt limit reached',updated=? WHERE job_id=?",
                            (time.time(),job['job_id']))
                self.event(con,job['job_id'],'attempt_limit')
            active = con.execute("SELECT COUNT(*) FROM jobs WHERE status='in_progress'").fetchone()[0]
            if con.execute("SELECT 1 FROM jobs WHERE worker_id=? AND status='in_progress'",(worker_id,)).fetchone():
                return {'claimed':False,'reason':'worker already owns an active job'}
            if active >= limit:
                return {'claimed':False,'reason':'local concurrency limit','active':active}
            row = con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created,job_id LIMIT 1").fetchone()
            if row is None:
                return {'claimed':False,'reason':'no queued jobs'}
            claim_id = uuid.uuid4().hex
            attempt_path = self.home/'attempts'/claim_id/('result'+Path(row['output_path']).suffix)
            attempt_path.parent.mkdir(parents=True)
            con.execute('''UPDATE jobs SET status='in_progress',claim_id=?,worker_id=?,attempt=attempt+1,
                           attempt_path=?,updated=?,error=NULL WHERE job_id=?''',
                        (claim_id,worker_id,str(attempt_path),time.time(),row['job_id']))
            self.event(con,row['job_id'],'claimed',claim_id=claim_id,worker_id=worker_id)
            spec = json.loads(row['spec'])
            return {'claimed':True,'job_id':row['job_id'],'claim_id':claim_id,'worker_id':worker_id,
                    'attempt':row['attempt']+1,'generation_output_path':str(attempt_path),
                    'next_item':spec['items'][0]}

    def active(self, con, claim_id):
        row = con.execute("SELECT * FROM jobs WHERE claim_id=? AND status='in_progress'",(claim_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown, revoked or already finished claim_id')
        return row

    def heartbeat(self, claim_id):
        with self.connection(True) as con:
            row = self.active(con,claim_id)
            con.execute('UPDATE jobs SET updated=? WHERE job_id=?',(time.time(),row['job_id']))
        return {'claim_id':claim_id,'alive':True}

    def publish(self, con, row, source):
        source = Path(source).expanduser().resolve()
        inspect_image(source)
        candidate = Path(row['attempt_path'])
        if source != candidate:
            shutil.copyfile(source,candidate)
        sha = digest(candidate)
        final = Path(row['output_path'])
        final.parent.mkdir(parents=True,exist_ok=True)
        # Preserve an existing file. It is adoptable only when it matches this attempt.
        if final.exists():
            if digest(final)!=sha:
                raise ValueError('Output conflict: existing final file differs; preserved unchanged')
        else:
            temp = final.parent / ('.image-queue-'+row['claim_id']+'.tmp')
            try:
                with candidate.open('rb') as src, temp.open('xb') as dst:
                    shutil.copyfileobj(src,dst)
                    dst.flush()
                    os.fsync(dst.fileno())
                # No-replace publication. The temp and final are on the same filesystem.
                os.link(temp,final)
            finally:
                temp.unlink(missing_ok=True)
        con.execute("UPDATE jobs SET status='completed',updated=?,result_sha256=?,error=NULL WHERE job_id=?",
                    (time.time(),sha,row['job_id']))
        self.event(con,row['job_id'],'completed',claim_id=row['claim_id'],sha256=sha)
        return {'job_id':row['job_id'],'status':'completed','output_path':str(final),'sha256':sha}

    def complete(self, claim_id, source=None):
        with self.connection(True) as con:
            old = con.execute('SELECT * FROM jobs WHERE claim_id=?',(claim_id,)).fetchone()
            if old and old['status']=='completed':
                if not Path(old['output_path']).is_file() or digest(old['output_path'])!=old['result_sha256']:
                    raise ValueError('Completed output has changed or disappeared')
                return {'job_id':old['job_id'],'status':'completed','already_completed':True,
                        'output_path':old['output_path'],'sha256':old['result_sha256']}
            row = self.active(con,claim_id)
            return self.publish(con,row,source or row['attempt_path'])

    def fail(self, claim_id, error):
        if not error.strip():
            raise ValueError('An error reason is required')
        with self.connection(True) as con:
            row = self.active(con,claim_id)
            con.execute("UPDATE jobs SET status='failed',error=?,updated=? WHERE job_id=?",
                        (error,time.time(),row['job_id']))
            self.event(con,row['job_id'],'failed',claim_id=claim_id,error=error)
            return {'job_id':row['job_id'],'status':'failed','error':error}

    def retry(self, job_id):
        with self.connection(True) as con:
            row = con.execute("SELECT * FROM jobs WHERE job_id=? AND status='failed'",(job_id,)).fetchone()
            if row is None:
                raise ValueError('Only failed jobs can be retried')
            if Path(row['output_path']).exists():
                raise ValueError('Output exists: inspect it before retry; use a new job/path if needed')
            return self.requeue(con,row,'manual_retry')

    def requeue(self, con, row, reason):
        limit = con.execute("SELECT value FROM settings WHERE key='max_attempts'").fetchone()[0]
        if row['attempt']>=limit:
            con.execute("UPDATE jobs SET status='failed',claim_id=NULL,error='attempt limit reached',updated=? WHERE job_id=?",
                        (time.time(),row['job_id']))
            self.event(con,row['job_id'],'attempt_limit',old_claim_id=row['claim_id'])
            return {'job_id':row['job_id'],'status':'failed','reason':'attempt limit reached'}
        con.execute("UPDATE jobs SET status='queued',claim_id=NULL,worker_id=NULL,updated=? WHERE job_id=?",
                    (time.time(),row['job_id']))
        self.event(con,row['job_id'],reason,old_claim_id=row['claim_id'])
        return {'job_id':row['job_id'],'status':'queued'}

    def recover(self, claim_id, confirmed=False):
        with self.connection(True) as con:
            row = self.active(con,claim_id)
            if not confirmed:
                return {'dry_run':True,'job_id':row['job_id'],'claim_id':claim_id,
                        'candidate_exists':Path(row['attempt_path']).is_file(),
                        'final_exists':Path(row['output_path']).exists(),
                        'next_step':'Confirm the old worker has stopped, then use --confirm-worker-stopped'}
            if Path(row['attempt_path']).is_file():
                return self.publish(con,row,row['attempt_path'])
            if Path(row['output_path']).exists():
                raise ValueError('Final file exists without this attempt candidate; inspect it manually')
            return self.requeue(con,row,'recovered_after_stop_confirmation')

    def status(self, job_id=None, stale_seconds=3600):
        with self.connection() as con:
            rows=con.execute('SELECT * FROM jobs'+(' WHERE job_id=?' if job_id else '')+' ORDER BY created',
                             (job_id,) if job_id else ()).fetchall()
            jobs=[]
            for row in rows:
                d=dict(row)
                d['spec']=json.loads(d['spec'])
                d['stale']=d['status']=='in_progress' and time.time()-d['updated']>=stale_seconds
                jobs.append(d)
            counts={}
            for j in jobs:counts[j['status']]=counts.get(j['status'],0)+1
            return {'home':str(self.home),'counts':counts,'jobs':jobs}


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version',action='version',version=VERSION)
    p.add_argument('--home',default=os.environ.get('CODEX_IMAGE_QUEUE_HOME',str(Path.home()/'.codex-image-queue')),
                   help='Shared local queue folder. All producer/worker windows must use the same folder.')
    sub=p.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init');init.add_argument('--max-active',type=int);init.add_argument('--max-attempts',type=int)
    en=sub.add_parser('enqueue');en.add_argument('--job-file',required=True);en.add_argument('--base-dir',default='.')
    for name in ['claim','watch']:
        c=sub.add_parser(name);c.add_argument('--worker-id',required=True)
        if name=='watch':c.add_argument('--timeout',type=float,default=60);c.add_argument('--poll',type=float,default=3)
    for name in ['complete','heartbeat','fail','recover']:
        c=sub.add_parser(name);c.add_argument('--claim-id',required=True)
        if name=='complete':c.add_argument('--source',help='Local image path returned by the generation tool')
        if name=='fail':c.add_argument('--error',required=True)
        if name=='recover':c.add_argument('--confirm-worker-stopped',action='store_true')
    c=sub.add_parser('retry');c.add_argument('--job-id',required=True)
    c=sub.add_parser('status');c.add_argument('--job-id');c.add_argument('--stale-seconds',type=float,default=3600)
    c=sub.add_parser('wait');c.add_argument('--job-id',required=True);c.add_argument('--timeout',type=float,default=60);c.add_argument('--poll',type=float,default=3)
    return p


def dispatch(q,a):
    if a.command=='init':return q.config(a.max_active,a.max_attempts)
    if a.command=='enqueue':return q.enqueue(json.loads(Path(a.job_file).read_text(encoding='utf-8-sig')),a.base_dir)
    if a.command=='claim':return q.claim(a.worker_id)
    if a.command=='heartbeat':return q.heartbeat(a.claim_id)
    if a.command=='complete':return q.complete(a.claim_id,a.source)
    if a.command=='fail':return q.fail(a.claim_id,a.error)
    if a.command=='retry':return q.retry(a.job_id)
    if a.command=='recover':return q.recover(a.claim_id,a.confirm_worker_stopped)
    if a.command=='status':return q.status(a.job_id,a.stale_seconds)
    if a.timeout<0 or a.poll<=0:raise ValueError('timeout must be nonnegative; poll must be positive')
    deadline=time.monotonic()+a.timeout
    while True:
        if a.command=='watch':
            result=q.claim(a.worker_id)
            if result['claimed'] or result['reason']=='worker already owns an active job':return result
        else:
            result=q.status(a.job_id)
            if not result['jobs']:raise ValueError('Unknown job_id')
            if result['jobs'][0]['status'] in ['completed','failed']:return result
        if time.monotonic()>=deadline:return dict(result,timed_out=True)
        time.sleep(min(a.poll,max(0,deadline-time.monotonic())))


def main():
    a=parser().parse_args()
    try:
        result=dispatch(Queue(a.home),a)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (ValueError,OSError,sqlite3.Error,json.JSONDecodeError) as exc:
        print(json.dumps({'error':str(exc),'type':type(exc).__name__},ensure_ascii=False))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
