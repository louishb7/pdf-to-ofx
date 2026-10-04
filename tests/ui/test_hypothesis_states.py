"""Hypothesis outcomes stay in the core; GUI only presents or blocks them."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

from pdf_to_ofx.application.convert import ExportStatus
from pdf_to_ofx.domain.evidence import AnalysisStatus

LAYOUTS = Path(__file__).parents[1] / 'fixtures/layouts'


@pytest.mark.parametrize('recipe', ['descending_checkpoints', 'daily_checkpoints', 'sparse_checkpoints'])
def test_checkpoint_hypotheses_visible_without_account_metadata(window, tmp_path, recipe):
    path = tmp_path / 'fictitious.pdf'
    spec=importlib.util.spec_from_file_location('hypothesis_fixture_writer',LAYOUTS/'generate.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_layout_pdf(LAYOUTS / recipe / 'pages.json',path)
    with patch('pdf_to_ofx.generic.parser.GenericStatementParser.interpret',side_effect=AssertionError('Legacy grammar')):
        window.load_pdf(path)
    assert window.analysis.status == AnalysisStatus.SUCCESS
    assert window.analysis.candidate_hypotheses > 0
    assert window.table.rowCount() == (3 if recipe.startswith('descending') else 5)
    assert window.export_readiness.status == ExportStatus.EXPORT_METADATA_REQUIRED
    assert not window.save_button.isEnabled()
    assert window.conversion_result is None


def test_two_financially_valid_hypotheses_are_blocked_in_gui(window, write_pdf, tmp_path):
    path=tmp_path / 'ambiguous.pdf'
    write_pdf('Período: 01/03/2027 a 03/03/2027\nSaldo inicial: R$ 100,00\n'
              'Saldo final: R$ 110,00\nOPERAÇÃO 01/03/2027 02/03/2027 R$ 10,00',path)
    window.load_pdf(path)
    assert window.analysis.status == AnalysisStatus.AMBIGUOUS
    assert window.analysis.candidate_hypotheses == 2
    assert window.table.rowCount() == 0
    assert not window.save_button.isEnabled()
    assert window.conversion_result is None
