"""生成 50 题评测集 v2：字段与 review_set_20261104.json 对齐，语料指纹调用 corpus_snapshot(db)。

问题由人阅读金标准块正文后撰写（未调用 LLM），答案均取自对应块内容。
块用 8 位 id 前缀指定，脚本按前缀唯一匹配，匹配不到/不唯一直接报错退出。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.db.session import session_scope  # noqa: E402
from scripts.prepare_review_set import corpus_snapshot  # noqa: E402

OUT = Path(r"D:\AI\RAG_project\data\eval\review_set_v2.json")

# (前缀, 问题, 参考答案, 分层)
SPECS = [
    # ============ 指南/共识 20 题 ============
    ("3f6e7481", "直肠癌病人有 MRI 禁忌，改做 CT 增强来判断分期，哪些方面 CT 其实看不太准？", "CT 判断壁外血管侵犯（EMVI）、潜在环周切缘（CRM）及低位直肠周围肛管复合体价值有限", "guideline"),
    ("7df76449", "局部进展期直肠癌到底什么情况下必须先做术前放化疗，什么情况可以直接开刀？", "T1~2N0M0 或有放化疗禁忌者推荐直接手术；T4 或局部晚期不可切除者必须术前放化疗；T3 和（或）N+ 可切除者原则上推荐术前新辅助治疗", "guideline"),
    ("de504e9e", "结肠癌同时有可切除的肝转移，术前化疗一般选什么方案、要不要加靶向、做多久？", "CapeOx/FOLFOX/FOLFIRI/FOLFOXIRI，靶向可选西妥昔单抗（RAS、NRAS、BRAF 野生型）或贝伐珠单抗；建议治疗时限 2~3 个月", "guideline"),
    ("c0149aa7", "结直肠癌肝转移病人做基因检测，RAS 到底要测哪些外显子？还建议查什么？", "KRAS 第 2、3、4 外显子以及 NRAS 第 2、3、4 外显子；另推荐 BRAF、MMR/MSI、UGT1A1、HER2 检测", "guideline"),
    ("6b785bcf", "肝转移一线用了 FOLFOX 之后进展了，二线怎么换？", "可改用 FOLFIRI（或 Mxeliri）±分子靶向；反之亦然，仍可考虑靶向药物；dMMR/MSI-H 未用过免疫者可优先免疫检查点抑制剂", "guideline"),
    ("2869ec03", "机器人 NOSES Ⅰ 式对肿瘤下缘距齿状线、浸润深度和最大横径分别有什么要求？", "肿瘤下缘距齿状线 2~5 cm 为宜，浸润深度 T3 以内，最大横直径≤5 cm", "guideline"),
    ("6745b039", "做 NOSES 之前怎么给直肠分段？哪些情况算相对禁忌？", "以齿状线为界：5 cm 以内下段、5~10 cm 中段、10 cm 以上上段；相对禁忌包括病灶较大、系膜肥厚、过度肥胖、肛门失禁或肛门功能明显减退", "guideline"),
    ("31689ccf", "侧方淋巴结清扫最容易出哪些并发症？没做术前放化疗、术后病理才发现侧方阳性怎么办？", "Ⅲ~Ⅴ级并发症发生率 8.5%，总并发症 16.4%；夹闭主要淋巴管、侧方间隙与 TME 平面连通、置引流管可降低淋巴囊肿；未行 NCRT 且术后病理阳性者局部复发率 22.2%~56.8%，推荐追加 NCRT/放化疗（A 级推荐）", "guideline"),
    ("0c922f23", "术中做盆腔自主神经监测，能把术后泌尿和性功能障碍降下来吗？有随机试验数据吗？", "有，欧洲多中心 RCT（NCT01585727）：神经监测组术后泌尿功能障碍 8%，对照组 19%；并降低排便功能障碍，男性勃起功能指数更高", "guideline"),
    ("24cdcfff", "直肠后侧方游离时走哪个间隙才能把神经保住？到侧韧带又该怎么处理？", "走直肠固有筋膜与腹下神经前筋膜之间的直肠后间隙，向外侧延续至侧韧带；紧贴直肠固有筋膜离断 IHP 到直肠的分支，保留侧韧带外侧段并注意直肠中动脉", "guideline"),
    ("9da85140", "侧方淋巴结到底多大算可疑？短径不到 7 mm 还有机会判阳性吗？", "初诊短径≥7 mm 作为主要临床诊断依据；短径不足 7 mm 但有 2 个以上恶性影像学特征（混杂信号、形态不规则、圆形）也可临床诊断阳性", "guideline"),
    ("edf3151f", "机器人结直肠手术的绝对禁忌证和相对禁忌证分别是什么？", "绝对禁忌证：严重心肺疾病无法耐受麻醉和创伤、结直肠癌广泛远处转移且无法完全清除；相对禁忌证：凝血机制异常（血小板减少、凝血因子缺乏或抗凝药物）经纠正后需谨慎权衡", "guideline"),
    ("a5a0bfbc", "腹膜转移什么情况下可以考虑做肿瘤细胞减灭术？术后残余怎么评分？", "PCI≤20 时可考虑 CRS；用 CC 评分评估残余：CC-0 无可见结节、CC-1 <2.5 mm、CC-2 2.5 mm~2.5 cm、CC-3 >2.5 cm 或不可切除融合病灶；PCI>20 者预后明显更差", "guideline"),
    ("04870015", "哪些结直肠癌病人术后容易发生腹膜转移？", "原发灶穿孔、伴同时性卵巢转移、原发灶非 R0 切除、T4 和（或）N+、淋巴结检出不足 12 枚、分化差（黏液腺癌等）、腹腔灌洗液游离癌细胞阳性", "guideline"),
    ("252e6082", "新辅助治疗后评效，cCR 第一次评估安排在什么时候？长程和短程放疗一样吗？", "初次评估宜在治疗后 12 周内完成：长程放疗结束后 8~12 周，短程放疗结束后或单纯药物治疗结束后 4~6 周", "guideline"),
    ("1df8da01", "局部切除后病理是 ypT0~1 和 ypT2 以上，后续处理有什么不同？", "无高危因素 ypT0~1 期完整切除即判定治愈并规范随访；有高危特征或 ypT2+ 推荐 TME（术后 8 周内）或辅助治疗，复发时需挽救手术", "guideline"),
    ("45b9b2f5", "粪便免疫化学检测和老的愈创木脂法比，好在哪？筛查间隔多久？", "FIT 敏感度和特异度更好，不受饮食药物影响，推荐每年 1 次；不再推荐 gFOBT 用于早期筛查", "guideline"),
    ("9b1aa982", "结肠镜发现不同数量和大小的腺瘤，随访间隔差多少？", "高风险腺瘤（≥10 mm 无蒂锯齿状或伴细胞学发育不良）每 3 年；1~2 个<10 mm 管状腺瘤或无蒂锯齿状息肉每 5~10 年；3~4 个每 3~5 年；5~10 个每 3 年；>10 个每年或遗传咨询", "guideline"),
    ("dfd94572", "机器人手术做到一半，什么情况要果断中转开腹？", "腹腔广泛粘连难以安全分离、难以控制的大出血、设备故障无法及时修复（机械臂失控、器械无法到达目标区域）、技术难题短时间无法解决", "guideline"),
    ("56b589c9", "肿瘤侵犯子宫附件和侵犯膀胱，机器人联合切除的处理原则有什么不同？", "仅子宫受侵犯可仅行子宫切除；单侧附件受侵犯建议双侧附件切除；膀胱受侵需评估三角及周围 2 cm、输尿管和受侵范围，遵循整块 en-bloc R0 切除并重视区域淋巴结清扫", "guideline"),

    # ============ 综述/研究 20 题 ============
    ("cc8c1801", "想在不做预防性造口的前提下降低中低位直肠癌吻合口漏，多技术整合都包括哪几项？", "限制性结肠脾曲游离（SFM）、保留左结肠动脉（LCA）、吻合口加固缝合、经肛管引流（TDT）", "review"),
    ("959fef9f", "结肠脾曲游离到底该做完全性还是限制性？理由是什么？", "推荐限制性 SFM：仅切断膈结肠韧带即可显露脾脏，保留脾结肠韧带、胃结肠韧带和胰腺-横结肠系膜韧带；完全性游离易致骶前肠管冗长、可能增加便秘风险", "review"),
    ("43195cc5", "经肛管引流要放几天？多技术整合在安全性上凭什么说好？", "肛管留置建议术后 5~7 d；多技术整合通过无张力吻合、保留 LCA 稳固血供、吻合口加固缝合、TDT 管控局部压力与引流共同构筑安全防线", "review"),
    ("15a1c652", "这套 Apple Vision Pro 教学系统，两种机器人做同样三步，用时和并发症有差别吗？", "达芬奇组各步骤用时及三步总用时均比康多组短（P<0.05）；两组术后均未发生切口感染、吻合口漏、肠梗阻；50 例学员满意度 100%", "review"),
    ("a75a4a38", "VR 训练和传统师徒带教比，到底能改善哪些指标？", "VR 训练在手术时间、路径长度、器械操作、组织处理、错误率及客观结构化技能评分上均显著优于传统训练", "review"),
    ("5842d08b", "结直肠微创这十几年是怎么一步步走过来的？单孔手术有什么优劣？", "从开腹到五孔腹腔镜、单孔、减孔，再到 NOSES、taTME；单孔仅一个切口，疼痛轻、恢复快、美容好，但操作三角小、器械易打架，难度大限制推广", "review"),
    ("aeb7631a", "达芬奇机器人和腹腔镜比，REAL 试验在环周切缘和并发症上给出的数字是多少？", "REAL Ⅲ期试验纳入 1 240 例中低位直肠癌：机器人组环周切缘阳性率更低（4.0% vs 7.2%，P=0.02），并发症更少（16.2% vs 23.1%，P=0.003）", "review"),
    ("22c3c5e4", "taTME 有什么隐忧？开展前要注意什么？", "手术经验不足可能导致更高局部复发率和并发症（有队列研究显示 taTME 局部复发率高于传统腹腔镜 TME）；开展前须经过严格规范培训", "review"),
    ("30adbc6a", "ISR 术后顽固性吻合口狭窄的发生率多少？哪些人是独立危险因素？", "495 例中 37 例发生狭窄（22 例顽固性）；多因素显示新辅助治疗、肿瘤距肛缘≤4 cm、临床 N 分期 N+ 是独立危险因素（风险比 7.297、3.898、2.672）", "review"),
    ("4ce23c5b", "放疗到底会不会增加吻合口狭窄？文献里为什么说法不一？", "存在争议：有 RCT 显示长程与短程放疗的吻合口狭窄等并发症无差异，但也有回顾性研究显示新辅助治疗增加放射性肠炎和吻合口狭窄风险；机制为放疗致局部水肿、纤维化、瘢痕愈合", "review"),
    ("9c5fda3f", "这套直肠癌自动分割加影像组学的流水线，分割精度和预测 pCR 的 AUC 大概多少？", "112 例独立测试集：Dice 分别为 0.74（新辅助前 T2WI）、0.66（新辅助后 T2WI）、0.67（前 DWI）、0.46（后 DWI）；预测流水线平均 AUC 0.83±0.04", "review"),
    ("cb2ef6f6", "新辅助治疗前后 T2WI 上自动勾画的稳定性差别有多大？", "新辅助前 T2WI 平均 Dice 0.74±0.09，新辅助后 T2WI 降至 0.66±0.13；新辅助后 DWI 仅 0.46±0.15，说明跨模态泛化能力差异明显", "review"),
    ("7d4a4075", "自动分割出来的影像组学特征，和人工勾画的结果对得上吗？", "一阶和形状特征在自动与人工勾画间均显著相关（多数 r 0.45~0.94，p<0.01），说明大模型辅助分割的组学特征具有稳健性", "review"),
    ("2ed18706", "光声成像和普通超声比，判断新辅助治疗后是否残留病灶谁更强？", "PAM 单独 AUC 0.947，明显优于 US 单独 0.881，早期融合仅小幅提升到 0.956；US 依赖声阻抗差，易被治疗后纤维化干扰", "review"),
    ("4968c3b8", "新辅助治疗后靠 T2WI 判断有没有残留，敏感度到底够不够？", "T2WI 区分 T0 与 T1~4 平均敏感度仅 19.1%（特异度 94.6%）；加 DWI 后敏感度改善、读片者依赖性降低；经验≥5 年读片者敏感度 70% vs 47.4%", "review"),
    ("a8c544e7", "这套光声/超声的前瞻研究，作者自己承认哪些局限？", "样本量小、需更大独立队列验证；数据来自本实验室自研系统与探头，无法验证域偏移；MRI T2WI 组学模型 AUC 0.877（95%CI 0.795~0.958）", "review"),
    ("3ac2a315", "加了知识库以后，模型判断手术耐受性的准确率提高多少？关键因素是知识库还是模型本身？", "DeepSeek-R1+KB 最佳（准确率 58.6%，TPR 59.1%，FNR 40.9%）；知识库仅带来 0.3%~1.4% 提升，模型架构（R1 vs Chat）才是代差的关键因素", "review"),
    ("0b560063", "用大模型打 ASA 评分靠谱吗？各模型准确率如何？", "四个模型组 ASA 评分准确率均低于 60%，组间无统计学差异；作者提醒基于回顾性数据、无法捕捉非医疗因素，结论需谨慎", "review"),
    ("878feda6", "手术解剖结构识别里，注意力网络和传统 CNN 各有什么取舍？", "CNN 帧率最高（87~100 FPS），注意力网络分割质量更好（Dice 最高 0.72 vs CNN 0.62）；SegNeXt 以 25 FPS 兼顾实时部署", "review"),
    ("34eacc7f", "手术阶段识别最好能做到多少准确率？难点在哪？", "最佳模型阶段识别准确率 82.8%；RAMIE 纵隔空间狭窄、阶段顺序随术者习惯和病变而变，需要强骨干网络加时序建模", "review"),

    # ============ 关系型 10 题 ============
    ("ac1c788f", "局部进展期直肠癌的长程放化疗、短程放疗和 TNT 模式，各自是什么关系？", "术前 T3 及以上或任何 T 分期淋巴结阳性者可用联合放化疗或放疗，包括长程联合放化疗和单纯短程放疗；TNT 把术后辅助化疗提至术前，完全缓解率更高、利于器官保留、降低远处转移（2a 类证据，B 级推荐）", "relational"),
    ("fd3a1db7", "机器人 NOSES 经肛门取标本（Ⅵ 式）和经阴道取标本（Ⅶ 式）适应证差在哪？", "Ⅵ 式：横结肠近脾曲、脾曲、降结肠和乙状结肠近端，T3 以内，最大横径≤5 cm，经肛门拉出；Ⅶ 式：同样部位但限女性，最大横径放宽到≤7 cm，经阴道拉出", "relational"),
    ("3ed0de94", "侧方淋巴结清扫，按筋膜走和按血管走，对排尿和生殖功能影响差多少？", "筋膜导向较血管导向显著降低术后排尿功能障碍（22.0% vs 45.3%，P=0.015）及生殖功能障碍（42.9% vs 74.3%，P=0.019）；共识优先推荐以输尿管腹下神经筋膜、膀胱腹下筋膜及盆腔壁层筋膜为导向", "relational"),
    ("bc3ba148", "新辅助放化疗和单纯手术比，术后性功能和排便功能会差多少？", "荟萃分析（33 项研究、17 917 例）显示新辅助治疗增加勃起功能障碍（OR=1.77）和排便功能障碍（OR=3.09）；新辅助放疗可导致泌尿、性功能和排便功能障碍，影响生命质量（Ⅱb 级）", "relational"),
    ("54ed6203", "局部切除、局部切除后追加 TME、直接 TME，三种做法的并发症差多少？", "GRECCAR-2 试验：并发症发生率分别为 29%、38%、78%；严重并发症（Clavien-Dindo Ⅲ~Ⅴ级）为 12%、22%、46%；三者肿瘤学结局无明显差异；LE 后补救 TME 的系膜切除不完整率更高", "relational"),
    ("46b84ad7", "NOSES 的适应证跟 2019 版比放宽了哪些？", "病灶最大环周直径：经直肠肛门由<3 cm 扩展为<5 cm，经阴道取标本由<5 cm 调整为 5~7 cm 为宜；肿瘤较大、系膜肥厚、肥胖（BMI≥35）、局部晚期等改为有经验团队选择性开展而非绝对禁忌", "relational"),
    ("ac9a14d8", "为什么用两根 26F 而不是一根 28F？双肛管怎么放、什么时候拔？", "单根 28F 常管腔阻塞、引流不畅且硬度更高，故改用双根 26F 硅胶管；需石蜡油润滑后在腹腔镜直视引导下缓慢插入，术后恢复饮食、排气排便良好后 5~7 d 拔除", "relational"),
    ("4e29152b", "Ⅱ 期结肠癌低危、普危、高危三档，术后辅助治疗分别怎么定？", "低危（T3N0M0、dMMR）不推荐辅助治疗；普危（T3N0M0、pMMR 无高危因素）单药氟尿嘧啶或随访观察；高危（T3N0M0 pMMR 伴高危因素或 T4N0M0）辅助化疗，优先 3 个月 CapeOX", "relational"),
    ("93ee41dc", "腹膜转移是怎么形成的？癌细胞从原发灶到腹膜有哪几条路？", "按种子土壤学说：癌细胞突破浆膜脱落、黏附腹膜、侵袭并新生血管形成转移灶；来源包括突破浆膜自然脱落、经血管/淋巴管/淋巴结脱落、术中挤压牵拉的医源性播散", "relational"),
    ("de37463c", "NOSES 和常规腹腔镜在取标本这一步有什么本质区别？这次调查是怎么做的？", "常规腹腔镜需在腹壁另作辅助小切口取标本；NOSES 经人体自然腔道（直肠肛门/阴道）取标本，更微创；本次为微信发放电子问卷的横断面调查", "relational"),
]


def main() -> int:
    with session_scope() as db:
        snapshot = corpus_snapshot(db)
        items = []
        for n, (prefix, question, answer, qtype) in enumerate(SPECS, 1):
            rows = db.execute(text('''
                SELECT c.id, c.document_id, c.content, c.page_start, c.phase,
                       d.title, d.doi, c.evidence_level,
                       (d.created_at >= '2026-11-14') AS new_doc
                FROM kb_chunk c JOIN kb_document d ON d.id = c.document_id
                WHERE d.is_active AND c.id::text LIKE :p'''), {'p': prefix + '%'}).mappings().all()
            if len(rows) != 1:
                print(f'  x {prefix} 匹配 {len(rows)} 条，需唯一')
                return 1
            r = rows[0]
            content = r['content']
            items.append({
                'qid': f'REVIEWV2-{n:02}',
                'question': question,
                'gold_chunk_id': str(r['id']),
                'gold_chunk_ids': [str(r['id'])],
                'gold_document_id': str(r['document_id']),
                'gold_doc_title': r['title'],
                'gold_page': r['page_start'],
                'phase': r['phase'],
                'doi': r['doi'],
                'gold_span': content,
                'reference_answer': answer,
                'content_sha256': hashlib.sha256(content.encode()).hexdigest(),
                'evidence_level': r['evidence_level'],
                'from_new_document': bool(r['new_doc']),
                'qtype': qtype,
                'review_status': 'pending_independent_clinical_review',
                'reviewer': None,
            })

    zh = sum(1 for i in items if any('\u4e00' <= c <= '\u9fff' for c in i['gold_span'][:200]))
    doc = {
        'name': 'Current-corpus source-grounded development review set (v2, 50 items)',
        'version': '2026-11-14.1',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'corpus': snapshot,
        'items': items,
        'limitations': ('50 source-selected development questions (20 guideline/consensus, 20 review/research, '
                        '10 relational), single-positive labels, no expert approval; no clinical accuracy claim. '
                        'Questions were written by reading the gold chunk text; gold_span is the full chunk, '
                        'so length is a chunk property, not a curated quote.'),
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({
        'items': len(items), 'corpus': snapshot, 'zh': zh, 'en': len(items) - zh,
        'new_doc': sum(1 for i in items if i['from_new_document']),
        'qtype': {q: sum(1 for i in items if i['qtype'] == q) for q in ('guideline', 'review', 'relational')},
        'docs': len({i['gold_document_id'] for i in items}),
        'max_per_doc': max(sum(1 for i in items if i['gold_document_id'] == d) for d in {i['gold_document_id'] for i in items}),
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
