"""Change-event table for two-period change runs (implementation plan §9, DATA-MODEL change_events).

Everything else a change run persists already exists in 0001: ``analysis_runs.type = 'change'`` with
``period_b``, the outcome enum (incl. ``coregistration_failed``), ``analysis_run_inputs.period``, the
change artifact kinds (``delta``, ``z``, ``category``, ``event_label``, ``joint_valid``,
``vector_gpkg``), the change evidence kinds, ``evidence.confidence_band``, ``observed_period_b`` and
``geometry_artifact_id``.

``change_events`` carries no numeric and no geometry columns: areas, strength, support and flag counts
are Measurements; geometry and confidence band live on the linked evidence row, and the authoritative
geometry is the ``change_events.gpkg`` artifact referenced by ``evidence.geometry_artifact_id``.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE change_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  analysis_run_id uuid NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
  evidence_id text NOT NULL UNIQUE REFERENCES evidence(evidence_id) ON DELETE CASCADE,
  category text NOT NULL CHECK (category IN ('water_increase', 'water_decrease', 'built_bare_increase',
                                             'veg_loss', 'veg_gain', 'other_significant')),
  touches_aoi_boundary boolean NOT NULL
);
CREATE INDEX change_events_run_cat_ix ON change_events (analysis_run_id, category);
CREATE TRIGGER change_events_immutable BEFORE UPDATE OR DELETE ON change_events
  FOR EACH ROW EXECUTE FUNCTION aether_reject_mutation();
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS change_events;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
