"""Freeze source-grounded review questions against the current active corpus.

No model-generated gold answers or claims of expert approval. Existing output
is immutable by default: use --refresh deliberately after a corpus migration.
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import text
from app.core.config import PROJECT_ROOT
from app.db.session import session_scope

# Questions were written after reading the actual source excerpts. These are
# single-positive development labels, pending independent clinical review.
SPECS = [
 ('0d902628-61bd-48da-bb2d-4380b77c3338', '哪些直肠癌患者可以考虑新辅助治疗后的等待观察策略？', '达到临床完全缓解；适用人群、评效时机、随访和补救仍需评估'),
 ('c9981d64-8204-47ec-b411-60cc825b8685', '直肠癌MRI的DWI扫描范围如何设置，能直接复制T2WI的所有参数吗？', '复制横断面T2WI范围，不复制层厚和扫描视野'),
 ('1a0b1cb0-bc9c-40ed-b90f-4f8e1c89c188', '直肠癌新辅助治疗前的MRI肿瘤自动分割，增加伪标签训练数据有什么效果？', '该回顾性研究半监督模型较基线提高分割DSC；不能推断普遍临床有效性'),
 ('c09e596f-6de2-4267-ab82-0a99d9eb641f', '右半结肠癌标准D2清扫的内侧界可以选在哪里，与D3范围有什么不同？', 'D2可选SMV右侧界；文中D3中央清扫内侧至SMA左侧'),
 ('8bdac8c5-a68f-48f7-b044-0262e03cbaea', 'ICG评估吻合口血运时发现预切除线或吻合后灌注差，文献描述怎样处理？', '移至近端血运良好处；吻合后血运不良时切除并再次吻合'),
 ('eafc4f61-179e-44b2-8f6b-ea5836b3738e', '优化右半结肠切除术系膜裂孔缝合的研究采用什么设计和关闭方法？', '回顾性描述性研究26例；体外与腹腔镜联合缝合'),
 ('3f0ed4bf-82a4-4cd6-80c2-10d86f50d057', '识别肠系膜下动静脉的近实时分割研究如何划分数据以避免同一患者泄漏？', '按患者划分13例开发、3例内部留出；单中心概念验证'),
 ('3893afc9-7ed6-4eef-b1d0-783f67c613c7', '直肠手术吻合口漏如何定义，早期和迟发漏的时间界限是什么？', '吻合部肠壁缺损连通或吻合旁盆腔脓肿；术后30天界限'),
 ('223f80d8-daff-4a31-81da-192384d30692', '结直肠手术患者何时首次营养筛查，用什么工具，多久复查？', '就诊24小时内NRS-2002，住院每周，老年可用GNRI'),
 ('d8b3c001-cca3-4404-8288-7fcce981b5cb', '腹会阴联合切除与前切除的排尿和性功能风险有何差异，侧方清扫需保护什么？', 'APR风险较高；侧方清扫应重视盆腔自主神经保护'),
 ('05e07965-a2ed-4e10-b849-922a70b2430a', '预防性造口对吻合口漏发生和严重后果的作用是否相同？', '发生率影响有争议；可减少严重并发症、再手术和相关病死率'),
 ('12dff4d3-d09f-4e9d-b694-cf5eda141c02', 'IMA高位与低位结扎比较研究中，ICG用于评价什么，研究能证明因果吗？', '评估吻合灌注；380例回顾性分析，不能据此证明因果'),
]


def corpus_snapshot(db):
    rows = db.execute(text('''SELECT c.id,c.document_id,c.content,c.embedding_model,
        c.phase,c.evidence_level,d.title FROM kb_chunk c JOIN kb_document d
        ON d.id=c.document_id WHERE d.is_active ORDER BY c.id''')).all()
    serial = [[str(r[0]), str(r[1]), hashlib.sha256(r[2].encode()).hexdigest(), *r[3:]] for r in rows]
    digest = hashlib.sha256(json.dumps(serial, ensure_ascii=False).encode()).hexdigest()
    return {'sha256': digest, 'chunks': len(rows), 'documents': len({r[1] for r in rows})}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    target = PROJECT_ROOT / 'data/eval/review_set_20261104.json'
    if target.exists() and not args.refresh:
        print(f'Frozen set exists: {target}; unchanged')
        return
    items = []
    with session_scope() as db:
        snapshot = corpus_snapshot(db)
        for n, (cid, question, answer) in enumerate(SPECS, 1):
            row = db.execute(text('''SELECT c.*,d.title,d.doi FROM kb_chunk c
                JOIN kb_document d ON d.id=c.document_id WHERE c.id=:id AND d.is_active'''), {'id': cid}).mappings().one()
            items.append({'qid': f'REVIEW-{n:02}', 'question': question, 'gold_chunk_id': cid,
                'gold_document_id': str(row['document_id']), 'gold_doc_title': row['title'],
                'gold_page': row['page_start'], 'phase': row['phase'], 'doi': row['doi'],
                'gold_span': row['content'], 'reference_answer': answer,
                'content_sha256': hashlib.sha256(row['content'].encode()).hexdigest(),
                'review_status': 'pending_independent_clinical_review', 'reviewer': None})
    data = {'name': 'Current-corpus source-grounded development review set', 'version': '2026-11-04.1',
        'created_at': datetime.now(timezone.utc).isoformat(), 'corpus': snapshot, 'items': items,
        'limitations': '12 source-selected development questions, single-positive labels, no expert approval; no clinical accuracy claim.'}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'items':len(items), 'corpus': snapshot}, ensure_ascii=False))


if __name__ == '__main__':
    main()
