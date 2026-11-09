"""C-02 interpretation (2026-11-09): ``coreg_pcc_error`` is the raw phase-correlation error, a diagnostic only.

It is recorded exactly as skimage returns it, never gates, never feeds a confidence band, and is not a
normalised registration-quality or confidence score. The residual gate stays the only registration gate.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import numpy as np
import pytest

from aether import science
from aether.geo import coreg
from aether.geo.change.events import confidence_band
from aether.geo.coreg import assess_coregistration, coregistration_outcome
from tests.unit.test_change_rules_events import _coreg, _ref
from tests.unit.test_coregistration import pair

SRC = Path(__file__).resolve().parents[2] / "src" / "aether"


def _stub_estimator(monkeypatch: pytest.MonkeyPatch, shift: tuple[float, float], error: float) -> None:
    """Replace only skimage's return value; the gate, residual and diagnostics code run unchanged."""
    monkeypatch.setattr(coreg, "phase_cross_correlation", lambda a, b, **kw: (np.array(shift), error, 0.0))


def test_a_raw_error_is_recorded_exactly_as_returned_by_the_frozen_call(monkeypatch):
    seen: list[tuple[dict, float]] = []
    real = coreg.phase_cross_correlation

    def spy(a, b, **kw):
        shift, error, phasediff = real(a, b, **kw)
        seen.append((kw, error))
        return shift, error, phasediff

    monkeypatch.setattr(coreg, "phase_cross_correlation", spy)
    r = assess_coregistration(*pair(0.3, 0.0))
    ((kwargs, raw),) = seen
    assert kwargs == {"upsample_factor": 10} == {"upsample_factor": science.COREG_UPSAMPLE_FACTOR}  # frozen call
    assert r.pcc_error == float(raw)  # no rescaling, normalisation or transformation


def test_a_raw_error_is_near_one_for_reflectance_scale_inputs_whether_aligned_or_not():
    """The documented reason it is not a quality score (skimage default normalization="phase")."""
    aligned = assess_coregistration(*pair(0.0, 0.0))
    shifted = assess_coregistration(*pair(0.3, 0.0))
    assert aligned.pcc_error == pytest.approx(1.0, abs=1e-6)
    assert shifted.pcc_error == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("error", [0.0, 0.5, 1.0, 1e6, float("nan")])
def test_b_raw_error_never_gates(monkeypatch, error):
    _stub_estimator(monkeypatch, (0.0, 0.0), error)
    r = assess_coregistration(*pair(0.0, 0.0))
    assert r.status == "ok" and coregistration_outcome(r) is None
    assert r.pcc_error == error or (np.isnan(error) and np.isnan(r.pcc_error))


@pytest.mark.parametrize("error", [0.0, 1.0])
def test_d_residual_alone_decides_failure_and_shift_residual_are_unchanged(monkeypatch, error):
    _stub_estimator(monkeypatch, (0.6, 0.0), error)  # 0.6 px > 0.5 px gate, whatever the error says
    r = assess_coregistration(*pair(0.0, 0.0))
    assert (r.shift_dy_px, r.shift_dx_px, r.residual_px) == (0.6, 0.0, 0.6)
    assert r.status == "failed" and coregistration_outcome(r) == "coregistration_failed"
    assert science.COREG_MAX_RESIDUAL_PX.value == 0.5


@pytest.mark.parametrize(
    ("mode", "strength", "support", "status", "residual"),
    [
        ("robust", 5.0, 6, "ok", 0.3),
        ("robust", 5.0, 6, "ok", 0.31),
        ("robust", 3.5, 4, "ok", 0.0),
        ("robust", 3.4, 4, "ok", 0.0),
        ("robust", 9.0, 9, "not_assessable", None),
        ("absolute_only", 9.0, 9, "ok", 0.0),
    ],
)
def test_c_raw_error_never_changes_a_confidence_band(mode, strength, support, status, residual):
    base = _coreg(status, residual)
    bands = {
        confidence_band(_ref(mode), strength, support, support, dataclasses.replace(base, pcc_error=e))
        for e in (None, 0.0, 0.5, 1.0, 1e6)
    }
    assert len(bands) == 1


def test_c_raw_error_is_only_produced_and_recorded_never_read_by_decision_code():
    """Only the estimator (coreg.py) produces it and only evidence recording (change_persistence.py) reads it."""
    uses = {
        str(p.relative_to(SRC)): len(re.findall(r"\bpcc_error\b", p.read_text()))
        for p in SRC.rglob("*.py")
        if re.search(r"\bpcc_error\b", p.read_text())
    }
    assert set(uses) == {"geo/coreg.py", "services/change_persistence.py"}
    assert uses["services/change_persistence.py"] == 1
    persistence = (SRC / "services" / "change_persistence.py").read_text()
    assert 'scalar("coreg_pcc_error", c.pcc_error if c else None)' in persistence  # recorded as-is
