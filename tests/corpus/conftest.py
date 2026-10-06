"""Only generated, fictitious PDFs are used by Corpus Lab tests."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def corpus_manifest(tmp_path):
    writer = Path(__file__).parents[1] / "fixtures/corpus_lab/generate.py"
    spec = importlib.util.spec_from_file_location("corpus_fixture_writer", writer)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate_corpus(tmp_path / "fictitious")
