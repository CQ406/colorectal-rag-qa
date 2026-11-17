"""Case provenance and publication prerequisites, without external requests."""
import pytest
from fastapi import HTTPException
from app.api.v1.cases import _validate_source
from scripts.prepare_public_cases import parse_article


def source(**overrides):
    return {"source_title": "Published case", "source_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/",
            "source_license": "CC BY 4.0", "source_note": "Verbatim excerpt, Case presentation section",
            "review_note": "Source and license checked", **overrides}


@pytest.mark.parametrize("field", ["source_title", "source_url", "source_license", "source_note", "review_note"])
def test_publishing_requires_provenance_and_review(field):
    with pytest.raises(HTTPException) as error:
        _validate_source(source(**{field: " "}), publishing=True)
    assert error.value.status_code == 422


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///tmp/a", "https://name:password@example.com", "https:///missing", "https://[", "https://example.com:invalid"])
def test_source_url_rejects_non_web_or_credential_links(url):
    with pytest.raises(HTTPException):
        _validate_source(source(source_url=url), publishing=False)


def test_draft_can_be_incomplete_but_valid_source_can_be_published():
    _validate_source({}, publishing=False)
    _validate_source(source(), publishing=True)


def test_open_case_parser_retains_source_and_does_not_invent_clinical_data():
    raw = b'''<article><front><article-meta><article-id pub-id-type="doi">10.1234/test</article-id>
    <title-group><article-title>Published case report</article-title></title-group>
    <permissions><license xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</license></permissions>
    </article-meta></front><body><sec><title>Case presentation</title><p>A patient underwent colectomy.</p></sec></body></article>'''
    entry = parse_article(raw, "PMC123", "intraop", "Case label")
    assert entry["is_public"] is False
    assert entry["age"] is None and entry["sex"] is None
    assert "A patient underwent colectomy." in entry["history"]
    assert entry["source_doi"] == "10.1234/test"
    assert "SHA256" in entry["source_note"]
    assert entry["image_ids"] == []


def test_open_case_parser_rejects_missing_or_noncommercial_license():
    raw = b'<article><front><article-meta><permissions><license href="https://creativecommons.org/licenses/by-nc/4.0/">CC BY NC</license></permissions></article-meta></front></article>'
    with pytest.raises(ValueError, match="license"):
        parse_article(raw, "PMC123", "preop", "Case")
