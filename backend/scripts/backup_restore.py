"""Local backup and isolated restore rehearsal. Never restores over the live DB.

Database tools run inside the project's PostgreSQL container; shell redirection
is deliberately avoided to preserve binary pg_dump output on PowerShell 5.1.
Secrets, .env, model weights and browser test databases are excluded.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from app.core.config import PROJECT_ROOT, settings
from app.db.session import engine
from app.services.graph_client import GraphClient


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def run(args, **kwargs):
    result = subprocess.run(args, stderr=subprocess.PIPE, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args[0]} {args[1]} failed: {result.stderr.decode(errors="replace")[:600]}')
    return result


def postgres(*args):
    return ['docker','exec','-i','cr-postgres', *args]


def table_counts(db_engine):
    names = sorted(inspect(db_engine).get_table_names())
    with db_engine.connect() as conn:
        return {name:conn.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one() for name in names}


def graph_export(target):
    with GraphClient() as client:
        nodes = client.run('MATCH (n) RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props')
        relationships = client.run('MATCH (a)-[r]->(b) RETURN elementId(a) AS source, elementId(b) AS target, type(r) AS type, properties(r) AS props')
    target.write_text(json.dumps({'nodes':nodes, 'relationships':relationships},ensure_ascii=False,indent=2),encoding='utf-8')
    return {'nodes':len(nodes), 'relationships':len(relationships)}


def backup():
    root = PROJECT_ROOT / 'data/backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    with (root/'database.dump').open('wb') as stream:
        run(postgres('pg_dump','-U',settings.postgres.postgres_user,'-d',settings.postgres.postgres_db,
            '--format=custom','--no-owner','--no-acl'), stdout=stream)
    # Extract counts from the dump itself during the rehearsal. Live counts are
    # informational because online backups can span concurrent writes.
    manifest = {'created_at':datetime.now(timezone.utc).isoformat(), 'database':settings.postgres.postgres_db,
        'online_backup':True, 'live_counts_after_dump':table_counts(engine), 'files':[],
        'exclusions':['.env and secrets','model binaries','Redis transient cache','E2E fixtures'],
        'consistency':'pg_dump is transactionally consistent; filesystem/graph snapshots are separate. Quiesce writes for a coordinated production backup.'}
    for relative in ('data/storage','data/manual','data/cases','data/document_sources','data/reviews'):
        source = PROJECT_ROOT / relative
        if source.exists():
            shutil.copytree(source, root/'workspace'/relative)
    eval_root = PROJECT_ROOT/'data/eval'
    for pattern in ('review_*.json','graph_extraction_cache.json','graph_current*.json',
                    'graph_source_audit*.json','graph_refresh_transfer_plan*.json','image_baseline*.json','live_restart*.json'):
        for path in eval_root.glob(pattern):
            destination = root/'workspace/data/eval'/path.name
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,destination)
    manifest['graph'] = graph_export(root/'graph.json')
    for path in sorted(root.rglob('*')):
        if path.is_file():
            manifest['files'].append({'path':path.relative_to(root).as_posix(), 'bytes':path.stat().st_size, 'sha256':sha256(path)})
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Backup created: {root}',flush=True)
    return root


def checked_files(root):
    allowed = (PROJECT_ROOT/'data/backups').resolve()
    root = root.resolve()
    if root == allowed or not root.is_relative_to(allowed):
        raise ValueError('Backup must be inside the project data/backups directory')
    manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    for item in manifest['files']:
        path = (root/item['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file() or sha256(path)!=item['sha256']:
            raise ValueError(f'Backup file missing, changed or unsafe: {item["path"]}')
    return manifest


def restore_graph(graph_path, uri):
    """Logical import only into a newly started empty rehearsal container."""
    graph = json.loads(graph_path.read_text(encoding='utf-8'))
    from neo4j import GraphDatabase
    with GraphDatabase.driver(uri, auth=None) as driver:
        driver.verify_connectivity()
        with driver.session() as session:
            if session.run('MATCH (n) RETURN count(n) AS n').single()['n']:
                raise ValueError('Graph restore destination must be empty')
            session.run('CREATE CONSTRAINT restore_id_unique IF NOT EXISTS FOR (n:RestoreNode) REQUIRE n.backup_id IS UNIQUE').consume()
            for offset in range(0,len(graph['nodes']),200):
                session.run('UNWIND $rows AS r CREATE (n:RestoreNode {backup_id:r.id}) SET n += r.props',rows=graph['nodes'][offset:offset+200]).consume()
            for label in sorted({label for n in graph['nodes'] for label in n['labels']}):
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',label):
                    raise ValueError('Unexpected graph label')
                ids = [n['id'] for n in graph['nodes'] if label in n['labels']]
                session.run(f'MATCH (n:RestoreNode) WHERE n.backup_id IN $ids SET n:`{label}`',ids=ids).consume()
            for rel_type in sorted({r['type'] for r in graph['relationships']}):
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',rel_type):
                    raise ValueError('Unexpected graph relationship type')
                rows = [r for r in graph['relationships'] if r['type']==rel_type]
                session.run(f'UNWIND $rows AS r MATCH (a:RestoreNode {{backup_id:r.source}}), (b:RestoreNode {{backup_id:r.target}}) CREATE (a)-[e:`{rel_type}`]->(b) SET e += r.props',rows=rows).consume()
            nodes = session.run('MATCH (n) RETURN count(n) AS n').single()['n']
            relationships = session.run('MATCH ()-[r]->() RETURN count(r) AS n').single()['n']
            if nodes != len(graph['nodes']) or relationships != len(graph['relationships']):
                raise ValueError('Graph restore count mismatch')
            # Verify every property and original label, not just object counts.
            restored_nodes = session.run('MATCH (n:RestoreNode) RETURN n.backup_id AS id, labels(n) AS labels, properties(n) AS props').data()
            expected = {n['id']:n for n in graph['nodes']}
            for node in restored_nodes:
                props = dict(node['props']); props.pop('backup_id',None)
                labels = [label for label in node['labels'] if label != 'RestoreNode']
                original = expected[node['id']]
                if props != original['props'] or sorted(labels)!=sorted(original['labels']):
                    raise ValueError('Graph property verification failed')
            restored_rels = session.run('MATCH (a:RestoreNode)-[r]->(b:RestoreNode) RETURN a.backup_id AS source, b.backup_id AS target, type(r) AS type, properties(r) AS props').data()
            canonical = lambda values: sorted(json.dumps(v,sort_keys=True,ensure_ascii=False) for v in values)
            if canonical(restored_rels) != canonical(graph['relationships']):
                raise ValueError('Graph relationship property verification failed')
            session.run('MATCH (n:RestoreNode) REMOVE n.backup_id REMOVE n:RestoreNode').consume()
            return {'nodes':nodes,'relationships':relationships,'node_properties_verified':True,'relationship_properties_verified':True}


def rehearse(root, with_graph=False):
    manifest = checked_files(root)
    suffix = uuid.uuid4().hex[:12]
    name = 'rag_restore_'+suffix
    if not re.fullmatch(r'rag_restore_[0-9a-f]{12}',name) or name == settings.postgres.postgres_db:
        raise ValueError('Invalid rehearsal database name')
    admin = create_engine(settings.postgres.dsn,isolation_level='AUTOCOMMIT')
    restored = None
    container = None
    container_id = None
    stage = PROJECT_ROOT/'data/restore-rehearsal'/suffix
    result = {'backup':root.name,'database':name,'started_at':datetime.now(timezone.utc).isoformat()}
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        with (root/'database.dump').open('rb') as stream:
            run(postgres('pg_restore','-U',settings.postgres.postgres_user,'-d',name,'--no-owner','--no-acl','--exit-on-error'),stdin=stream,stdout=subprocess.PIPE)
        restored = create_engine(engine.url.set(database=name))
        result['restored_table_counts'] = table_counts(restored)
        result['counts_match_live_snapshot'] = result['restored_table_counts']==manifest['live_counts_after_dump']
        if not result['counts_match_live_snapshot']:
            raise ValueError('Rehearsal count mismatch; check concurrent writes and dump integrity')
        shutil.copytree(root/'workspace',stage)
        files = [item for item in manifest['files'] if item['path'].startswith('workspace/')]
        for item in files:
            copy = stage / Path(item['path']).relative_to('workspace')
            if sha256(copy)!=item['sha256']:
                raise ValueError('Restored file checksum mismatch')
        result['restored_files_verified'] = len(files)
        if with_graph:
            container = 'rag-restore-'+suffix
            # Unique container; no production volumes, network or ports used.
            container_id = run(['docker','run','-d','--name',container,'--label','rag.rehearsal=true',
                '-p','127.0.0.1::7687','-e','NEO4J_AUTH=none','-e','NEO4J_server_memory_heap_initial__size=256m',
                '-e','NEO4J_server_memory_heap_max__size=512m','-e','NEO4J_server_memory_pagecache_size=128m',
                'neo4j:5-community'],stdout=subprocess.PIPE).stdout.decode().strip()
            import time
            uri = None
            for _ in range(40):
                port = run(['docker','port',container,'7687/tcp'],stdout=subprocess.PIPE).stdout.decode().strip().rsplit(':',1)[-1]
                uri = 'bolt://127.0.0.1:'+port
                try:
                    from neo4j import GraphDatabase
                    with GraphDatabase.driver(uri,auth=None,connection_timeout=2) as driver:
                        driver.verify_connectivity()
                    break
                except Exception:
                    time.sleep(2)
            else:
                raise RuntimeError('Isolated graph container did not become ready')
            result['graph_restore'] = restore_graph(root/'graph.json',uri)
        result['status'] = 'passed'
    finally:
        if restored is not None:
            restored.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        if container_id:
            identity = run(['docker','inspect','--format','{{.Id}}|{{index .Config.Labels "rag.rehearsal"}}',container],stdout=subprocess.PIPE).stdout.decode().strip()
            if identity != container_id+'|true':
                raise RuntimeError('Refusing to remove container with unexpected identity')
            run(['docker','rm','-f',container_id],stdout=subprocess.PIPE)
        result['isolated_database_removed'] = True
        result['isolated_container_removed'] = bool(container_id)
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        (root/'restore-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['backup','rehearse','backup-and-rehearse'])
    parser.add_argument('--backup',type=Path)
    parser.add_argument('--with-graph',action='store_true')
    args = parser.parse_args()
    root = backup() if args.command!='rehearse' else args.backup
    if args.command!='backup':
        if root is None:
            parser.error('--backup required for rehearse')
        rehearse(root,args.with_graph)


if __name__ == '__main__':
    main()
