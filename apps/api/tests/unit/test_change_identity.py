"""Change-run idempotency identity (implementation plan §10, JOB-SYSTEM C-10) and its separation from
the pinned Phase 1 snapshot key."""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest

from aether import science
from aether.domain.periods import Period
from aether.services.analyses import idempotency_key
from aether.services.change_run import change_idempotency_key
from tests.unit.test_phase1_identity import PHASE1_PARAMETERS, PHASE1_VERSIONS, REFERENCE_AOI, REFERENCE_KEY, REFERENCE_PERIOD

AOI = {"id": uuid.UUID("00000000-0000-0000-0000-000000000001"), "geometry_hash": "0" * 64}
A = Period(date(2023, 6, 1), date(2023, 8, 15))
B = Period(date(2024, 6, 1), date(2024, 8, 15))


def key(aoi=AOI, a=A, b=B):
    return change_idempotency_key(aoi, a, b)


def test_same_request_gives_the_same_identity():
    assert key() == key(dict(AOI), Period(A.start, A.end), Period(B.start, B.end))
    assert len(key()) == 64


@pytest.mark.parametrize(
    "variant",
    [
        {"a": Period(date(2023, 6, 2), date(2023, 8, 15))},
        {"b": Period(date(2024, 6, 1), date(2024, 8, 14))},
        {"aoi": {**AOI, "geometry_hash": "1" * 64}},
        {"aoi": {**AOI, "id": uuid.UUID("00000000-0000-0000-0000-000000000002")}},
    ],
)
def test_identity_is_sensitive_to_periods_and_aoi(variant):
    assert key(**variant) != key()


def test_identity_is_sensitive_to_collection(monkeypatch):
    base = key()
    monkeypatch.setattr(science, "STAC_COLLECTION", "sentinel-2-l2a")
    assert key() != base


@pytest.mark.parametrize("entry", ["change", "coreg", "vector", "indices"])
def test_identity_is_sensitive_to_every_change_version(monkeypatch, entry):
    base = key()
    monkeypatch.setattr(science, "CHANGE_ALGORITHM_VERSIONS", {**science.CHANGE_ALGORITHM_VERSIONS, entry: "x.v2"})
    assert key() != base


def test_identity_is_sensitive_to_the_frozen_change_parameters(monkeypatch):
    base = key()
    monkeypatch.setattr(science, "CHANGE_K", science.Parameter("k", 3.5, science.Calibration.UNTUNED, ""))
    assert key() != base


def test_change_parameters_are_snapshot_parameters_plus_frozen_change_defaults():
    p = science.change_parameters()
    assert {k: p[k] for k in PHASE1_PARAMETERS} == PHASE1_PARAMETERS
    assert {k: v for k, v in p.items() if k not in PHASE1_PARAMETERS} == {
        "k": 3.0,
        "a_NDVI": 0.15,
        "a_MNDWI": 0.20,
        "a_NDBI": 0.10,
        "mmu_m2": 5000,
        "widespread_z": 2.0,
        "widespread_frac": 0.30,
        "band_higher_min_strength": 5.0,
        "band_higher_min_support": 6,
        "band_higher_max_coreg_residual_px": 0.3,
        "band_moderate_min_strength": 3.5,
        "band_moderate_min_support": 4,
        "coreg_max_residual_px": 0.5,
        "coreg_min_window_px": 256,
        "coreg_min_grid_fraction": 0.5,
        "upsample_factor": 10,
    }


def test_phase1_snapshot_identity_is_unchanged_and_distinct():
    snap = idempotency_key(REFERENCE_AOI, "snapshot", REFERENCE_PERIOD, science.snapshot_parameters())
    assert snap == REFERENCE_KEY
    assert list(science.SNAPSHOT_ALGORITHM_VERSIONS.items()) == PHASE1_VERSIONS
    assert science.snapshot_parameters() == PHASE1_PARAMETERS
    assert change_idempotency_key(REFERENCE_AOI, REFERENCE_PERIOD, B) != snap
    assert json.dumps(science.parameter_calibration_table()) == json.dumps(
        [
            {"name": "n_min", "value": 3, "calibration": "untuned", "note": "Initial engineering default (C5 §9)"},
            {"name": "stats_min_support_fraction", "value": 0.5, "calibration": "untuned", "note": "C4 §5 unavailable rule"},
            {"name": "coverage_insufficient_below", "value": 0.5, "calibration": "untuned", "note": "C6 §5 policy default"},
            {"name": "coverage_partial_below", "value": 0.8, "calibration": "untuned", "note": "C6 §5 policy default"},
        ]
    )
