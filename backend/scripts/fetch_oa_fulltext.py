"""从 Europe PMC 拉开放获取文献的**全文 XML**，转成本项目可入库的 Markdown。

为什么用 XML 而不是 PDF
----------------------
1. **可脚本化**：Europe PMC 的 `?pdf=render` 链接对程序请求返回 403（反爬），
   而 `/rest/{PMCID}/fullTextXML` 稳定返回 200 与完整 JATS 全文。
2. **质量更高**：PDF 抽取会丢章节层级；某些 PDF 字体缺 ToUnicode，
   抽出来是 `!"#$%` 乱码（本项目踩过）。XML 自带 `<sec>` 结构，章节名可直接入元数据。
3. **恰好贴合本项目切块方式**：`build_chunks` 按 `##` 章节切分，
   XML 的 `<sec>` 一一对应，引用能定位到"第几节"。

输出格式与 `app/etl/manual_import.py::parse_markdown` 完全兼容：

    # 文章标题
    **作者**：...
    **期刊**：...
    **年份**：...
    **DOI**：...
    **语种**：en

    ## Introduction
    正文段落...

    ## Methods
    ...

用法
----
    # 只列命中，不下载
    set PMC_QUERY=TITLE_ABS:"colorectal" AND TITLE_ABS:"anastomotic leak"
    python scripts/fetch_oa_fulltext.py --limit 10 --dry-run

    # 下载并转换，写到 data/incoming_md/
    python scripts/fetch_oa_fulltext.py --limit 10
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import time
from pathlib import Path

import httpx
from lxml import etree

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UA = "colorectal-rag-thesis/1.0 (academic research; local)"
OUT_DIR = Path(r"D:\AI\RAG_project\data\incoming_md")
MANIFEST = OUT_DIR / "manifest.csv"
FIELDS = ["pmcid", "pmid", "doi", "title", "journal", "year", "md_path", "chars", "sections", "status"]


def client() -> httpx.Client:
    return httpx.Client(timeout=90.0, follow_redirects=True, trust_env=False,
                        headers={"User-Agent": UA})


def search(c: httpx.Client, query: str, page_size: int) -> list[dict]:
    """检索有 PMCID 的 OA 文献（只有 PMC 收录的才有全文 XML）。"""
    params = {
        "query": f"({query}) AND OPEN_ACCESS:y AND SRC:PMC",
        "format": "json",
        "pageSize": min(page_size, 100),
        "resultType": "core",
        "sort": "CITED desc",
    }
    r = c.get(f"{BASE}/search", params=params)
    r.raise_for_status()
    return r.json().get("resultList", {}).get("result", [])


def _txt(el) -> str:
    """节点全文本，压缩空白。"""
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _sec_to_md(sec, depth: int = 2) -> list[str]:
    """把 JATS <sec> 递归转成 Markdown 标题 + 段落。"""
    out: list[str] = []
    title = _txt(sec.find("title"))
    if title:
        level = "#" * min(depth, 4)
        out.append(f"{level} {title}")
        out.append("")
    for child in sec:
        tag = etree.QName(child).localname
        if tag == "p":
            t = _txt(child)
            if t:
                out.append(t)
                out.append("")
        elif tag == "sec":
            out.extend(_sec_to_md(child, depth + 1))
        elif tag in ("table-wrap", "fig"):
            cap = _txt(child.find("caption"))
            if cap:
                out.append(f"> [{tag}] {cap}")
                out.append("")
    return out


def jats_to_markdown(xml_text: str, item: dict) -> tuple[str, int]:
    """JATS XML → 本项目 Markdown。返回（markdown, 章节数）。"""
    root = etree.fromstring(xml_text.encode("utf-8"))

    # 题录
    title = _txt(root.find(".//article-meta//title-group/article-title"))
    if not title:
        title = item.get("title") or "Untitled"
    journal = _txt(root.find(".//journal-meta//journal-title"))
    year = ""
    for pd in root.findall(".//article-meta//pub-date"):
        y = _txt(pd.find("year"))
        if y:
            year = y
            break
    doi = ""
    for aid in root.findall(".//article-meta//article-id"):
        if aid.get("pub-id-type") == "doi":
            doi = _txt(aid)
            break
    authors = []
    # 只取 contrib-type="author"：JATS 里 contrib 也会包含编辑（例如 Cureus 的
    # Alexander Muacevic / John R Adler 是栏目编辑，不是作者），不过滤会张冠李戴。
    for c in root.findall(".//article-meta//contrib-group/contrib"):
        if (c.get("contrib-type") or "").lower() not in ("author", ""):
            continue
        sn, gn = _txt(c.find(".//surname")), _txt(c.find(".//given-names"))
        if sn:
            authors.append(f"{gn} {sn}".strip())
    author_str = ", ".join(authors[:8]) + (" 等" if len(authors) > 8 else "")

    lines = [f"# {title}", ""]
    if author_str:
        lines.append(f"**作者**：{author_str}")
    if journal:
        lines.append(f"**期刊**：{journal}")
    if year:
        lines.append(f"**年份**：{year}")
    if doi:
        lines.append(f"**DOI**：{doi}")
    lines.append("**语种**：en")
    lines.append("")

    # 摘要
    abstract = _txt(root.find(".//article-meta//abstract"))
    if abstract:
        lines += ["## Abstract", "", abstract, ""]

    # 正文
    n_sec = 0
    body = root.find(".//body")
    if body is not None:
        for child in body:
            tag = etree.QName(child).localname
            if tag == "sec":
                block = _sec_to_md(child)
                if block:
                    n_sec += 1
                    lines.extend(block)
            elif tag == "p":
                t = _txt(child)
                if t:
                    lines += [t, ""]

    return "\n".join(lines).strip() + "\n", n_sec


def safe_name(pmcid: str, title: str, max_len: int = 60) -> str:
    keep = "".join(ch for ch in (title or "") if ch not in '\\/:*?"<>|')
    keep = " ".join(keep.split())[:max_len].rstrip(" .")
    return f"{pmcid}_{keep or 'untitled'}.md"


def load_manifest() -> dict[str, dict]:
    if not MANIFEST.exists():
        return {}
    with MANIFEST.open("r", encoding="utf-8-sig", newline="") as f:
        return {r["pmcid"]: r for r in csv.DictReader(f)}


def save_manifest(rows: dict[str, dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows.values():
            w.writerow({k: r.get(k, "") for k in FIELDS})


def main() -> int:
    ap = argparse.ArgumentParser(description="拉 Europe PMC 全文 XML 并转为 Markdown")
    ap.add_argument("--query", default=None, help="检索式；也可用环境变量 PMC_QUERY")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--min-chars", type=int, default=2000, help="正文字符下限（质量闸门）")
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    query = args.query or os.environ.get("PMC_QUERY", "").strip()
    if not query:
        ap.error("需要 --query，或设置环境变量 PMC_QUERY")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_manifest()
    got = 0

    with client() as c:
        items = search(c, query, args.limit * 3)
        print(f"候选 {len(items)} 条（有 PMCID、开放获取、按被引降序）\n")

        for it in items:
            if got >= args.limit:
                break
            pmcid = it.get("pmcid")
            title = it.get("title") or ""
            if not pmcid:
                continue
            if args.skip_existing and rows.get(pmcid, {}).get("status") == "ok":
                print(f"  跳过（已有） {pmcid} {title[:48]}")
                continue

            if args.dry_run:
                print(f"  [dry] {pmcid}  PMID {it.get('pmid','')}  {title[:56]}")
                got += 1
                continue

            try:
                r = c.get(f"{BASE}/{pmcid}/fullTextXML")
                r.raise_for_status()
                md, n_sec = jats_to_markdown(r.text, it)
            except Exception as exc:  # noqa: BLE001
                print(f"  失败        {pmcid} {str(exc)[:60]}")
                rows[pmcid] = {"pmcid": pmcid, "pmid": it.get("pmid", ""),
                               "title": title, "status": f"failed:{type(exc).__name__}"}
                continue

            chars = len(md)
            ok = chars >= args.min_chars and n_sec >= 2
            if ok:
                path = OUT_DIR / safe_name(pmcid, title)
                path.write_text(md, encoding="utf-8")
                rows[pmcid] = {
                    "pmcid": pmcid, "pmid": it.get("pmid", ""), "doi": it.get("doi", ""),
                    "title": title, "journal": it.get("journalTitle", ""),
                    "year": it.get("pubYear", ""), "md_path": str(path),
                    "chars": chars, "sections": n_sec, "status": "ok",
                }
                print(f"  ✓ {pmcid}  章节 {n_sec:>2}  字符 {chars:>6}  {title[:44]}")
            else:
                rows[pmcid] = {"pmcid": pmcid, "pmid": it.get("pmid", ""),
                               "title": title, "chars": chars, "sections": n_sec,
                               "status": "too_short"}
                print(f"  ✗ 内容过少   {pmcid}  章节 {n_sec}  字符 {chars}")

            got += 1
            save_manifest(rows)
            time.sleep(args.delay)

    if not args.dry_run:
        save_manifest(rows)
        ok_n = sum(1 for r in rows.values() if r.get("status") == "ok")
        print(f"\n完成：本次 {got} 篇；累计 ok {ok_n} 篇 → {OUT_DIR}")
        print("下一步：把 .md 移到 data/manual/ 或直接跑入库脚本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
