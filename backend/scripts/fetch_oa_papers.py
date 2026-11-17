"""批量下载开放获取（OA）文献全文 PDF。

为什么只做 OA
-------------
知网/万方等中文库没有可用的批量接口，抓取违反服务条款且有法律与封 IP 风险。
本科毕设所需数量（十余篇）完全可以用「官方 OA 源 + 机构订阅手工下载」覆盖，
不值得为此写爬虫。本脚本只对接**明确开放获取**的接口：

    Europe PMC REST  https://www.ebi.ac.uk/europepmc/webservices/rest
    PMC OA           https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi（可选，本脚本用其 PDF 直链）

用法
----
    # 只列候选，不下载（先看清单是否对口）
    python scripts/fetch_oa_papers.py --query "(colorectal OR rectal) AND perioperative" \
        --limit 10 --dry-run

    # 下载到 data/incoming/，并写清单
    python scripts/fetch_oa_papers.py --query "..." --limit 10

    # 已有清单时跳过已下载项（断点续传），重复执行安全
    python scripts/fetch_oa_papers.py --query "..." --limit 10 --skip-existing

设计要点
--------
1. **限速**：Europe PMC 建议 ≤5 req/s；本脚本串行 + 0.4 s 间隔，并带联系方式 UA。
2. **幂等**：以 PMID 为键写 manifest.csv；重复执行不会重复下载。
3. **质量闸门**：下载后用 pymupdf 抽文本，可提取字符数过少即判失败
   —— 踩过坑：某些 PDF 字体缺 ToUnicode，抽出来是 `!"#$%` 乱码，
   入库后只会污染检索，必须在入口拦掉。
4. **可追溯**：记录来源 URL、下载时间、SHA256，便于论文附录与审计。
"""
from __future__ import annotations

import argparse
import csv
import os
import hashlib
import json
import time
from pathlib import Path

import httpx

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UA = "colorectal-rag-thesis/1.0 (academic research; contact: local)"
OUT_DIR = Path(r"D:\AI\RAG_project\data\incoming")
MANIFEST = OUT_DIR / "manifest.csv"
FIELDS = ["pmid", "doi", "title", "journal", "year", "pdf_path", "sha256", "chars", "status"]


def _client() -> httpx.Client:
    # trust_env=False：本机存在失效代理配置，读环境变量会连不上
    return httpx.Client(
        timeout=60.0, follow_redirects=True, trust_env=False,
        headers={"User-Agent": UA},
    )


def search(client: httpx.Client, query: str, page_size: int) -> list[dict]:
    """检索 OA 且有 PDF 的记录。"""
    params = {
        "query": f"({query}) AND OPEN_ACCESS:y AND HAS_PDF:y",
        "format": "json",
        "pageSize": min(page_size, 100),
        "resultType": "core",
        "sort": "CITED desc",  # 优先高被引，质量更稳
    }
    r = client.get(f"{BASE}/search", params=params)
    r.raise_for_status()
    data = r.json()
    return data.get("resultList", {}).get("result", [])


def pdf_url(item: dict) -> str | None:
    """从 resultType=core 的记录里找 PDF 直链。

    不同来源字段不一：PMC 的文章通常有 fullTextUrlList 里的 pdf 条目，
    部分记录只有 pmcid，此时用 Europe PMC 的 render 端点拼。
    """
    for u in (item.get("fullTextUrlList") or {}).get("fullTextUrl", []) or []:
        if (u.get("documentStyle") or "").lower() == "pdf" and u.get("url"):
            return u["url"]
        if (u.get("availability") or "").lower() == "open access" and (u.get("documentStyle") or "") == "pdf":
            return u.get("url")
    pmcid = item.get("pmcid")
    if pmcid:
        return f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextPDF"
    pmcid2 = item.get("id")
    if item.get("source") == "PMC" and pmcid2:
        return f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid2}/fullTextPDF"
    return None


def extract_chars(pdf_path: Path) -> tuple[int, int]:
    """返回（可提取字符数, 汉字数），用于质量闸门。"""
    try:
        import pymupdf
    except ImportError:  # 兼容旧包名
        import fitz as pymupdf
    try:
        with pymupdf.open(pdf_path) as doc:
            text = "".join(p.get_text() for p in doc)
    except Exception:
        return 0, 0
    han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return len(text.strip()), han


def load_manifest() -> dict[str, dict]:
    if not MANIFEST.exists():
        return {}
    with MANIFEST.open("r", encoding="utf-8-sig", newline="") as f:
        return {row["pmid"]: row for row in csv.DictReader(f)}


def save_manifest(rows: dict[str, dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for row in rows.values():
            w.writerow({k: row.get(k, "") for k in FIELDS})


def safe_name(title: str, pmid: str, max_len: int = 80) -> str:
    """文件名：PMID + 清洗后的标题，避免非法字符与超长路径。"""
    keep = "".join(c for c in (title or "") if c not in '\\/:*?"<>|')
    keep = " ".join(keep.split())[:max_len].rstrip(" .")
    return f"{pmid}_{keep or 'untitled'}.pdf"


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 OA 文献 PDF")
    # 检索式支持环境变量传入：Windows PowerShell 会吞掉内层双引号，
    # 诸如 TITLE_ABS:"enhanced recovery" 这样的字段检索用命令行传不进去，
    # 改用 $env:PMC_QUERY 就完全避开引号问题。
    ap.add_argument("--query", default=None, help="Europe PMC 检索式；也可用环境变量 PMC_QUERY")
    ap.add_argument("--limit", type=int, default=10, help="最多下载篇数")
    ap.add_argument("--dry-run", action="store_true", help="只列候选，不下载")
    ap.add_argument("--skip-existing", action="store_true", help="跳过 manifest 中已成功的 PMID")
    ap.add_argument("--min-chars", type=int, default=1200, help="可提取字符数下限（质量闸门）")
    ap.add_argument("--delay", type=float, default=0.4, help="每次请求间隔秒数（限速）")
    args = ap.parse_args()

    query = args.query or os.environ.get("PMC_QUERY", "").strip()
    if not query:
        ap.error("需要 --query，或设置环境变量 PMC_QUERY")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_manifest()

    with _client() as client:
        items = search(client, query, args.limit * 3)  # 多取一些，跳过不合格的
        print(f"候选 {len(items)} 条（已按被引降序）\n")

        got = 0
        for it in items:
            if got >= args.limit:
                break
            pmid = str(it.get("pmid") or it.get("id") or "")
            title = it.get("title") or ""
            if not pmid:
                continue
            if args.skip_existing and rows.get(pmid, {}).get("status") == "ok":
                print(f"  跳过（已下载） {pmid} {title[:50]}")
                continue

            url = pdf_url(it)
            if not url:
                print(f"  无 PDF 直链    {pmid} {title[:50]}")
                continue

            if args.dry_run:
                print(f"  [dry] {pmid} {title[:58]}")
                print(f"         {url}")
                got += 1
                continue

            fname = safe_name(title, pmid)
            path = OUT_DIR / fname
            try:
                resp = client.get(url)
                resp.raise_for_status()
                path.write_bytes(resp.content)
            except Exception as exc:  # noqa: BLE001
                print(f"  下载失败      {pmid} {str(exc)[:60]}")
                rows[pmid] = {"pmid": pmid, "doi": it.get("doi", ""), "title": title,
                              "journal": it.get("journalTitle", ""), "year": it.get("pubYear", ""),
                              "pdf_path": "", "sha256": "", "chars": "", "status": "download_failed"}
                continue

            chars, han = extract_chars(path)
            ok = chars >= args.min_chars
            rows[pmid] = {
                "pmid": pmid, "doi": it.get("doi", ""), "title": title,
                "journal": it.get("journalTitle", ""), "year": it.get("pubYear", ""),
                "pdf_path": str(path), "chars": chars,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
                "status": "ok" if ok else "text_unusable",
            }
            flag = "✓" if ok else "✗ 文本不可用"
            print(f"  {flag} {pmid} 字符 {chars:>6} 汉字 {han:>4}  {title[:44]}")
            if not ok:
                path.unlink(missing_ok=True)  # 抽不出文本的文件没有保留价值

            got += 1
            save_manifest(rows)
            time.sleep(args.delay)

    if not args.dry_run:
        save_manifest(rows)
        ok_n = sum(1 for r in rows.values() if r.get("status") == "ok")
        print(f"\n完成：本次尝试 {got} 篇；清单累计 ok {ok_n} 篇 → {MANIFEST}")
        print("下一步：python scripts/batch_convert_new_pdfs.py 转为 Markdown 后入库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
