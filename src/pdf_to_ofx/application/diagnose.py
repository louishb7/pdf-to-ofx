"""Content-free diagnostics from the same analysis used by the GUI."""

from collections import Counter
from pathlib import Path

from pdf_to_ofx.application.convert import StatementAnalysis, analyze_pdf, assess_export_readiness
from pdf_to_ofx.domain.evidence import AnalysisStatus


def diagnose_pdf(path: Path) -> dict[str, object]:
    """Return bounded technical evidence, never amounts, identity or descriptions."""
    return diagnose_analysis(analyze_pdf(path))


def diagnose_analysis(analysis: StatementAnalysis) -> dict[str, object]:
    """Project an existing analysis without running the engine again."""
    diagnostics = analysis.monetary_diagnostics
    return {
        "status": analysis.status.value,
        "reason": analysis.reason,
        "diagnostic": analysis.diagnostic.value if analysis.diagnostic else None,
        "failure_stage": analysis.failure_stage,
        "blocking_capabilities": list(analysis.blocking_capabilities),
        "candidate_hypotheses": analysis.candidate_hypotheses,
        "explored_hypotheses": analysis.explored_hypotheses,
        "budget_exhausted": analysis.budget_exhausted,
        "pruned_constraints": dict(sorted(analysis.pruned_constraints, key=lambda item: (-item[1], item[0]))[:10]),
        "monetary_diagnostics": {
            "counts": dict(sorted(Counter(d.cause.value for d in diagnostics).items())),
            "locations": [
                {"page": d.source.page, "row": d.source.row,
                 "word_start": d.source.word_start, "word_end": d.source.word_end,
                 "cause": d.cause.value,
                 "rejected_rules": sorted({decision.rule for decision in d.decisions if not decision.admitted})}
                for d in diagnostics[:8]
            ],
            "omitted_locations": max(0, len(diagnostics) - 8),
        },
        "approved": analysis.status == AnalysisStatus.SUCCESS,
        "export_readiness": assess_export_readiness(analysis).status.value,
    }
