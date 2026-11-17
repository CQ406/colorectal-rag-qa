"""Fetch a fixed set of open case reports and produce attributed draft excerpts.

No LLM extraction, invented clinical details, image downloads, or publication.
Run from backend: python -m scripts.prepare_public_cases
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import PROJECT_ROOT

CANDIDATES = [
    ("PMC12672323", "preop", "肠旋转不良患者的结直肠癌腹腔镜手术"),
    ("PMC12335908", "postop", "结直肠癌腹腔镜术后乳糜腹水"),
    ("PMC12714418", "intraop", "乙状结肠切除中的血管变异"),
    ("PMC12310414", "preop", "合并肝硬化及直肠静脉曲张的直肠癌手术"),
    ("PMC11951576", "preop", "先天性巨结肠术后吻合部位的结直肠癌"),
    ("PMC12078836", "intraop", "吲哚菁绿荧光在直肠癌腹腔镜手术中的应用"),
    ("PMC11362403", "intraop", "乙状结肠右侧固定伴降结肠肿瘤的手术"),
    ("PMC11871790", "postop", "直肠切除及回肠造口术后艰难梭菌感染"),
    ("PMC12187830", "postop", "乙状结肠癌前切除术后输尿管结肠瘘"),
    ("PMC11514532", "postop", "直肠癌术后吻合口闭塞的内镜再通"),
    ("PMC11335538", "postop", "低位直肠前切除术后隐匿性吻合口狭窄"),
    ("PMC12257621", "intraop", "异物引起乙状结肠穿孔的腹腔镜修补"),
    ("PMC11758254", "intraop", "巨大直肠间质瘤的机器人及经肛手术"),
    ("PMC11527522", "postop", "机器人直肠切除术后手部骨筋膜室综合征"),
]


def text(node) -> str:
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip() if node is not None else ""


def parse_article(raw: bytes, pmcid: str, phase: str, label: str) -> dict:
    root = ET.fromstring(raw)
    license_node = root.find(".//article-meta/permissions/license")
    license_text = text(license_node)
    links = ([value for node in license_node.iter() for value in node.attrib.values()]
             + re.findall(r"https?://creativecommons\.org/licenses/[^\s<>]+", license_text)) if license_node is not None else []
    license_uri = next((url for url in links if "creativecommons.org/licenses/by/" in url), "")
    explicit_by = "Creative Commons Attribution License" in license_text and not re.search(r"NonCommercial|Non-Commercial|NoDeriv|ShareAlike|BY-NC|BY-ND", license_text, re.I)
    if not license_uri and not explicit_by:
        raise ValueError(f"{pmcid}: no explicitly verified CC BY license; skip")
    license_name = "CC BY " + license_uri.rstrip("/").split("/")[-1] if license_uri else "CC BY（原文未注明版本号）"
    sections = []
    for section in root.findall(".//body//sec"):
        heading = text(section.find("title"))
        if re.search(r"case (presentation|report|description)|presentation of case|patient information", heading, re.I):
            paragraphs = [text(p) for p in section.findall(".//p") if text(p)]
            if paragraphs:
                sections.append((heading, paragraphs))
    if not sections:
        raise ValueError(f"{pmcid}: no named case section; manual extraction required")
    heading, paragraphs = max(sections, key=lambda item: sum(map(len, item[1])))
    doi = next((text(node) for node in root.findall(".//article-meta/article-id") if node.get("pub-id-type") == "doi"), None)
    title = text(root.find(".//article-meta/title-group/article-title"))
    authors = "; ".join(" ".join(filter(None, [text(n.find("given-names")), text(n.find("surname"))])) for n in root.findall('.//article-meta/contrib-group/contrib/name'))
    return {
        "case_code": f"PUB-{pmcid}", "title": label,
        "phase": phase, "age": None, "sex": None,
        "diagnosis": "见病例原文摘录；尚未进行结构化临床标注",
        "surgery": None,
        "history": "公开病例报告英文原文摘录（保留原意，尚待人工核对）：\n\n" + "\n\n".join(paragraphs),
        "findings": "原文资料见病史摘录；本字段尚未结构化，不代表原文未报告。",
        "operative_note": "原文资料见病史摘录；本字段尚未结构化，不代表原文未报告。",
        "prognosis": "原文资料见病史摘录；本字段尚未结构化，不代表原文未报告。",
        "tags": ["公开文献摘录", "待人工核对", phase], "complications": [], "image_ids": [],
        "source_title": title, "source_url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
        "source_doi": doi, "source_license": license_name,
        "source_note": f"Europe PMC 全文 XML；作者：{authors}；章节：{heading}；许可：{license_uri or license_name}。原文病例章节逐段保留，中文标题为导航标签。未报告的个人结构字段不补写；未下载或关联原文图像。原始XML SHA256: {hashlib.sha256(raw).hexdigest()}。原文许可说明：{license_text}",
        "is_public": False,
    }


def main() -> int:
    destination = PROJECT_ROOT / 'data/cases/public_case_drafts.json'
    if destination.exists():
        print(f'Frozen source manifest exists; retained unchanged: {destination}')
        return 0
    directory = PROJECT_ROOT / "data" / "cases" / "sources"
    directory.mkdir(parents=True, exist_ok=True)
    cases, errors = [], []
    for pmcid, phase, label in CANDIDATES:
        if len(cases) >= 10:
            break
        path = directory / f"{pmcid}.xml"
        try:
            if path.exists():
                raw = path.read_bytes()
            else:
                url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
                with urllib.request.urlopen(url, timeout=40) as response:
                    raw = response.read()
                path.write_bytes(raw)
            cases.append(parse_article(raw, pmcid, phase, label))
            print(f"Prepared {pmcid}; phase={phase}", flush=True)
        except Exception as exc:
            errors.append({"pmcid": pmcid, "error": str(exc)})
            print(f"Skipped {pmcid}: {type(exc).__name__}: {exc}")
    destination = directory.parent / "public_case_drafts.json"
    destination.write_text(json.dumps({"prepared_at": datetime.now(timezone.utc).isoformat(),
        "status": "draft_excerpts_not_clinically_reviewed", "cases": cases, "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Prepared {len(cases)} attributed drafts: {destination}")
    return 0 if len(cases) == 10 else 1


if __name__ == "__main__":
    raise SystemExit(main())
