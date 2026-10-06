"""Technical diagnostics retain search evidence without retaining PDF contents."""

import json

import pytest

from conftest import LAYOUTS
from pdf_to_ofx.application.convert import analyze_pdf, export_analysis
from pdf_to_ofx.application.diagnose import diagnose_pdf
from pdf_to_ofx.cli import main
from pdf_to_ofx.domain.errors import UnsupportedLayoutError
from pdf_to_ofx.generic.inference import infer_layout
from pdf_to_ofx.pdf.extractor import extract_pdf


@pytest.mark.parametrize("fixture,expected_code", [
    ("wrapped_description_separators", 0),
    ("snapshot_and_dated_table", 1),
])
def test_diagnose_uses_application_result_without_printing_financial_contents(
    tmp_path, write_layout_pdf, capsys, fixture, expected_code,
):
    path = tmp_path / "PRIVATE_FILENAME.pdf"
    write_layout_pdf(LAYOUTS / fixture / "pages.json", path)
    analysis = analyze_pdf(path)
    inference = infer_layout(extract_pdf(path))
    assert analysis.explored_hypotheses == inference.explored_hypotheses
    assert analysis.pruned_constraints == inference.pruned_constraints
    assert analysis.monetary_diagnostics == inference.monetary_diagnostics
    assert main(["diagnose", str(path)]) == expected_code
    stdout = capsys.readouterr().out
    report = json.loads(stdout)
    assert report["status"] == analysis.status.value
    assert report["candidate_hypotheses"] == inference.candidate_hypotheses
    assert report["explored_hypotheses"] == inference.explored_hypotheses
    assert report["blocking_capabilities"] == list(inference.blocking_capabilities)
    for private in ("PRIVATE_FILENAME", "REFERENCIA", "UNIDADE", "17,25", "17.25", "ITEM FICTÍCIO", "17,00", "17.00"):
        assert private not in stdout
    assert not list(tmp_path.glob("*.ofx"))


def test_unowned_column_currency_caption_is_located_and_cannot_export(tmp_path, write_layout_pdf):
    path = tmp_path / "fictitious.pdf"
    write_layout_pdf(LAYOUTS / "snapshot_and_dated_table/pages.json", path)
    report = diagnose_pdf(path)
    assert report["status"] == "unsupported"
    assert report["failure_stage"] == "financial_context"
    assert report["blocking_capabilities"] == ["financial_caption_ownership"]
    assert report["candidate_hypotheses"] == report["explored_hypotheses"] == 0
    assert report["pruned_constraints"] == {}
    location = report["monetary_diagnostics"]["locations"][0]
    assert (location["page"], location["row"]) == (1, 5)
    assert report["export_readiness"] == "export_blocked"
    with pytest.raises(UnsupportedLayoutError):
        export_analysis(analyze_pdf(path))


def test_missing_pdf_diagnosis_does_not_expose_path(tmp_path, capsys):
    assert main(["diagnose", str(tmp_path / "PRIVATE_FILENAME.pdf")]) == 1
    output = capsys.readouterr().out
    assert "PRIVATE_FILENAME" not in output
    assert json.loads(output)["export_readiness"] == "export_blocked"


@pytest.mark.parametrize("failure", ["inventory", "constraint"])
def test_diagnosis_distinguishes_presearch_failure_from_eliminated_hypotheses(
    tmp_path, write_layout_pdf, failure,
):
    recipe = json.loads((LAYOUTS / "wrapped_description_separators/pages.json").read_text())
    if failure == "inventory":
        recipe["pages"][0][7]["cells"][1][1] = "UNPROVEN REFERENCE -"
    else:
        recipe["pages"][0][6]["cells"][2][1] = "+17,24"
    source = tmp_path / "recipe.json"
    source.write_text(json.dumps(recipe))
    pdf = tmp_path / "fictitious.pdf"
    write_layout_pdf(source, pdf)
    report = diagnose_pdf(pdf)
    assert not report["approved"]
    if failure == "inventory":
        assert report["failure_stage"] == "monetary_inventory"
        assert report["explored_hypotheses"] == 0
        assert report["pruned_constraints"] == {}
        assert report["monetary_diagnostics"]["counts"] == {"monetary_token_incomplete": 1}
    else:
        assert report["explored_hypotheses"] > 0
        assert report["pruned_constraints"]
        assert report["monetary_diagnostics"]["counts"] == {}
