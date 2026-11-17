"""Prepare local evidence dossiers; never approve, publish, or call an LLM."""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.config import PROJECT_ROOT
from app.db.session import session_scope
from app.models.knowledge import Chunk
from scripts.prepare_public_cases import parse_article, text
from scripts.prepare_review_set import corpus_snapshot


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def case_dossier(case: dict, raw: bytes) -> dict:
    pmcid = case['case_code'].removeprefix('PUB-')
    parsed = parse_article(raw, pmcid, case['phase'], case['title'])
    matches = {key: parsed[key] == case[key] for key in
               ('history', 'source_title', 'source_doi', 'source_license')}
    matches['xml_sha256_in_manifest'] = digest(raw) in case['source_note']
    if not all(matches.values()):
        raise ValueError(f'{pmcid}: frozen case no longer matches source XML: {matches}')
    root = ET.fromstring(raw)
    paragraphs = []
    # Include diagnostic/treatment/outcome siblings, but exclude discussion and
    # captions. Only direct paragraphs: nested sections are visited separately.
    for index, section in enumerate(root.findall('.//body//sec'), 1):
        heading = text(section.find('title'))
        if not re.search(r'case (presentation|report|description)|presentation of case|'
                         r'patient information|diagnostic assessment|therapeutic intervention|'
                         r'follow.up|outcome|patient perspective', heading, re.I):
            continue
        for number, node in enumerate(section.findall('./p'), 1):
            content = text(node)
            if not content:
                continue
            paragraphs.append({'id': f's{index}-p{number}', 'section': heading,
                               'text': content, 'sha256': digest(content.encode())})
    candidates = {}
    for paragraph in paragraphs:
        match = re.search(r'\b(\d{1,3})[- ]year[- ]old\s+(woman|man|male|female)\b',
                          paragraph['text'], re.I)
        if match:
            candidates['demographics'] = {
                'age': int(match[1]), 'sex': 'F' if match[2].lower() in ('woman', 'female') else 'M',
                'quote': match[0], 'paragraph_id': paragraph['id'],
                'status': 'text_extraction_pending_human_confirmation'}
            break
    # Evidence pointers are suggestions for navigation, not inferred diagnoses.
    patterns = {
        'diagnosis': r'diagnos|histolog|patholog|adenocarcinoma|GIST',
        'findings': r'\bCT\b|\bMRI\b|colonoscopy|examination|imaging',
        'operative_note': r'laparoscop|resect|anastomos|fasciotomy|dilatation|suture',
        'prognosis': r'follow.up|discharg|recover|recurrence|months|years',
    }
    for field, pattern in patterns.items():
        candidates[field] = {'paragraph_ids': [p['id'] for p in paragraphs
                                               if re.search(pattern, p['text'], re.I)],
                             'value': None, 'status': 'requires_human_structuring'}
    extra = [p['id'] for p in paragraphs if p['text'] not in case['history']]
    return {'case_code': case['case_code'], 'title': case['title'],
            'source_url': case['source_url'], 'source_doi': case['source_doi'],
            'source_title': case['source_title'], 'source_license': case['source_license'],
            'source_note': case['source_note'], 'xml_sha256': digest(raw),
            'source_checks': matches, 'paragraphs': paragraphs,
            'additional_evidence_not_in_frozen_excerpt': extra,
            'candidate_fields': candidates, 'image_ids': [],
            'review': {'status': 'pending_clinical_review', 'reviewer': None, 'reviewed_at': None,
                       'clinical_fields': None, 'license_confirmed': None,
                       'evidence_support': None, 'decision': None, 'notes': None}}


def question_dossier(item: dict, chunk: Chunk | None, results: list[dict]) -> dict:
    if chunk is None or digest(chunk.content.encode()) != item['content_sha256']:
        raise ValueError(f"{item['qid']}: missing or changed gold evidence")
    if str(chunk.document_id) != item['gold_document_id']:
        raise ValueError(f"{item['qid']}: source document mismatch")
    rankings = []
    for result in results:
        record = next(r for r in result['records'] if r['qid'] == item['qid'])
        rankings.append({'configuration': result['label'], 'gold_rank': record['rank'],
                         'returned_chunk_ids': record['chunk_ids']})
    return {**item, 'technical_source_check': 'passed', 'retrieval': rankings,
            'review': {'status': 'pending_clinical_review', 'reviewer': None, 'reviewed_at': None,
                       'reference_supported': None, 'population_and_limits': None,
                       'numeric_claims': None, 'additional_relevant_chunk_ids': [],
                       'actual_answer': None, 'answer_configuration': None,
                       'citation_support': None, 'insufficient_evidence_handling': None,
                       'decision': None, 'notes': None}}


def main():
    cases_path = PROJECT_ROOT / 'data/cases/public_case_drafts.json'
    dataset_path = PROJECT_ROOT / 'data/eval/review_set_20261104.json'
    ablation_path = PROJECT_ROOT / 'data/eval/review_ablation_20261105.json'
    cases = json.loads(cases_path.read_text(encoding='utf-8'))['cases']
    dataset = json.loads(dataset_path.read_text(encoding='utf-8'))
    ablation = json.loads(ablation_path.read_text(encoding='utf-8'))
    with session_scope() as db:
        if corpus_snapshot(db) != dataset['corpus'] or ablation['corpus'] != dataset['corpus']:
            raise ValueError('Corpus differs from frozen review evidence')
        questions = [question_dossier(item, db.scalar(select(Chunk).where(Chunk.id == item['gold_chunk_id'])),
                                      ablation['results']) for item in dataset['items']]
    dossiers = [case_dossier(case, (PROJECT_ROOT / 'data/cases/sources' /
                                  (case['case_code'].removeprefix('PUB-') + '.xml')).read_bytes())
                for case in cases]
    report = {'created_at': datetime.now(timezone.utc).isoformat(),
              'status': 'technical_checks_passed_clinical_review_pending',
              'corpus': dataset['corpus'], 'external_calls': 0, 'database_writes': 0,
              'inputs': {p.relative_to(PROJECT_ROOT).as_posix(): digest(p.read_bytes())
                         for p in (cases_path, dataset_path, ablation_path)},
              'cases': dossiers, 'questions': questions}
    # New directory on every run: never overwrite a reviewer's decisions.
    directory = PROJECT_ROOT / 'data/reviews' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    directory.mkdir(parents=True, exist_ok=False)
    (directory / 'review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 病例与问答复核材料', '',
             '本材料仅完成本地来源一致性检查和证据导航，未经临床审核。候选年龄/性别仅为原文文字抽取；其他字段由审核人根据原文填写。', '',
             '请复制 review.json 后填写 review 字段，记录姓名、日期、依据与异议；不要修改冻结输入。没有实际生成回答的题目，不评定引用正确性或拒答质量。', '']
    for case in dossiers:
        lines += [f"## {case['case_code']}：{case['title']}", '',
                  f"来源：{case['source_url']}；DOI：{case['source_doi']}；许可：{case['source_license']}", '',
                  f"XML SHA256：`{case['xml_sha256']}`", '',
                  '补充段落：' + (', '.join(case['additional_evidence_not_in_frozen_excerpt']) or '无'), '',
                  '候选人口学字段：`' + json.dumps(case['candidate_fields'].get('demographics'), ensure_ascii=False) + '`', '']
        for p in case['paragraphs']:
            lines += [f"### {p['id']} · {p['section']}", '', p['text'], '']
    for item in questions:
        lines += [f"## {item['qid']}：{item['question']}", '',
                  f"来源：{item['gold_doc_title']}；块 `{item['gold_chunk_id']}`", '',
                  '整理的参考要点（待审核）：' + item['reference_answer'], '',
                  '原文：', '', item['gold_span'], '',
                  '检索排名：' + '；'.join(f"{r['configuration']}={r['gold_rank']}" for r in item['retrieval']), '',
                  '审核人/日期、适用人群、数值、支持关系、实际回答、引用核对及结论：待填写。', '']
    (directory / 'review.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'directory': str(directory), 'cases': len(dossiers), 'questions': len(questions),
                      'cases_with_extra_evidence': [c['case_code'] for c in dossiers
                                                   if c['additional_evidence_not_in_frozen_excerpt']],
                      'status': report['status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
