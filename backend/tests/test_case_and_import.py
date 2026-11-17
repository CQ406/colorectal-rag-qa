"""病例库、DOCX 导入和影像基线特征的纯单元测试。"""

from pathlib import Path

import pytest

from app.etl.manual_import import parse_docx, parse_markdown, parse_pdf
from app.models.knowledge import CaseRecord
from app.repositories import case_repo
from app.repositories.case_repo import case_text
from app.services.image_features import extract_feature
from app.services.prompts import build_qa_prompt


def test_case_text_contains_structured_fields() -> None:
    case = CaseRecord(
        case_code="T-001",
        title="直肠癌病例",
        diagnosis="中低位直肠癌",
        surgery="腹腔镜直肠前切除",
        phase="postop",
        tags=["吻合口", "ERAS"],
    )
    text = case_text(case)
    assert "中低位直肠癌" in text
    assert "腹腔镜直肠前切除" in text
    assert "ERAS" in text


def test_case_embedding_text_excludes_nonsemantic_image_ids() -> None:
    case = CaseRecord(
        case_code="T-003", title="直肠癌病例", age=62, sex="F",
        diagnosis="直肠癌", phase="postop",
        image_ids=["f6cb5dc0-41cc-4988-a123-998e63a46572"],
    )
    text = case_text(case)
    assert "62" in text
    assert "f6cb5dc0-41cc-4988-a123-998e63a46572" not in text
    assert "image_ids" not in text


def test_markdown_metadata_language_is_preserved() -> None:
    entry = parse_markdown(
        "# A guideline\n**语种**：en\n## Recommendation\nUse evidence."
    )
    assert entry.language == "en"
    assert entry.sections == [("Recommendation", "Use evidence.")]


def test_docx_import_uses_heading_structure(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("结直肠手术指南", level=1)
    document.add_paragraph("**年份**：2025")
    document.add_heading("术前评估", level=2)
    document.add_paragraph("完成营养风险筛查。")
    path = tmp_path / "guide.docx"
    document.save(path)

    entry = parse_docx(path)
    assert entry.title == "结直肠手术指南"
    assert entry.sections == [("术前评估", "完成营养风险筛查。")]


def test_pdf_import_preserves_pages(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "guideline.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Colorectal Surgery Guideline 2025")
    document.save(path)
    document.close()

    entry = parse_pdf(path)
    assert "Colorectal Surgery Guideline" in entry.title
    assert entry.sections[0][0] == "第1页"
    assert "2025" in entry.sections[0][1]


def test_image_baseline_feature_has_512_dimensions() -> None:
    image = pytest.importorskip("PIL.Image")
    import io

    out = io.BytesIO()
    image.new("RGB", (4, 4), (255, 0, 0)).save(out, format="PNG")
    vector = extract_feature(out.getvalue())
    assert len(vector) == 512
    assert pytest.approx(sum(x * x for x in vector), abs=1e-6) == 1.0


def test_case_search_falls_back_to_chinese_term_matching(monkeypatch) -> None:
    case = CaseRecord(
        case_code="T-002", title="吻合口漏病例", diagnosis="术后吻合口漏",
        surgery="直肠前切除", phase="postop", is_public=True,
    )

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [case]

    class FakeDb:
        def execute(self, _statement):
            return Result()

    monkeypatch.setattr(case_repo, "_embedding", lambda _text: None)
    found = case_repo.search_similar(FakeDb(), "吻合口漏")  # type: ignore[arg-type]
    assert found and found[0][0] is case
    assert found[0][1] > 0


@pytest.mark.parametrize(
    ("phase", "expected"),
    [("preop", "术前评估目标"), ("intraop", "实时医嘱"), ("postop", "随访")],
)
def test_qa_prompt_adds_phase_specific_structure(phase: str, expected: str) -> None:
    prompt = build_qa_prompt("测试问题", [], phase=phase)
    assert expected in prompt
