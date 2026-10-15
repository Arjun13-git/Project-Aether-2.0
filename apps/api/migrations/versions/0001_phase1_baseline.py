"""Phase 1 baseline schema (DATA-MODEL.md, C7).

Creates the Phase 0–1 tables only. change_events (Phase 2), analyst_* and claims (Phase 3) and
reports (Phase 4) are created by their own phase migrations, so no table exists before the phase
that writes it.

Revision ID: 0001
Revises:
Create Date: 2026-10-13
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE EXTENSION IF NOT EXISTS postgis;

-- Immutability guard. Project deletion sets aether.project_delete = 'on' (transaction-local)
-- so the ownership cascade may remove immutable rows; nothing else can.
CREATE FUNCTION aether_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND current_setting('aether.project_delete', true) = 'on' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'immutable_row: % on % is not allowed', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'check_violation';
END $$;

CREATE TABLE projects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  description text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE aois (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  geom geometry(MultiPolygon, 4326) NOT NULL CHECK (ST_IsValid(geom)),
  geometry_hash text NOT NULL,
  source text NOT NULL CHECK (source IN ('drawn', 'geojson')),
  source_geojson jsonb NOT NULL,
  normalization text[] NOT NULL,
  utm_epsg int NOT NULL CHECK ((utm_epsg BETWEEN 32601 AND 32660) OR (utm_epsg BETWEEN 32701 AND 32760)),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, geometry_hash, name)
);
CREATE INDEX aois_geom_gix ON aois USING gist (geom);
CREATE INDEX aois_project_ix ON aois (project_id);
CREATE TRIGGER aois_immutable BEFORE UPDATE OR DELETE ON aois
  FOR EACH ROW EXECUTE FUNCTION aether_reject_mutation();

CREATE TABLE scene_searches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  aoi_id uuid NOT NULL REFERENCES aois(id) ON DELETE CASCADE,
  api_url text NOT NULL,
  collection text NOT NULL,
  request jsonb NOT NULL,
  item_ids text[] NOT NULL,
  number_matched int,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX scene_searches_aoi_ix ON scene_searches (aoi_id);

CREATE TABLE scenes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider text NOT NULL CHECK (provider = 'earth-search'),
  collection text NOT NULL,
  item_id text NOT NULL,
  stac_json jsonb NOT NULL,
  stac_sha256 text NOT NULL,
  stac_updated timestamptz,
  acquired_at timestamptz,
  platform text NOT NULL,
  processing_baseline text,
  datatake_id text,
  grid_code text,
  epsg int,
  tile_cloud_cover double precision,
  sun_elevation_deg double precision,
  sun_azimuth_deg double precision,
  incidence_angle_deg double precision,
  footprint geometry(Geometry, 4326),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, collection, item_id)
);
CREATE INDEX scenes_footprint_gix ON scenes USING gist (footprint);
CREATE INDEX scenes_acquired_ix ON scenes (acquired_at);
CREATE INDEX scenes_datatake_ix ON scenes (datatake_id);

CREATE TABLE assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scene_id uuid NOT NULL REFERENCES scenes(id) ON DELETE RESTRICT,
  asset_key text NOT NULL,
  href text NOT NULL,
  media_type text,
  raster_bands jsonb,
  file_checksum text,
  file_size bigint,
  proj_transform double precision[],
  proj_shape int[],
  UNIQUE (scene_id, asset_key)
);

CREATE TABLE analysis_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  aoi_id uuid NOT NULL REFERENCES aois(id) ON DELETE CASCADE,
  type text NOT NULL CHECK (type IN ('snapshot', 'change')),
  period_a daterange NOT NULL,
  period_b daterange,
  collection text NOT NULL,
  parameters jsonb NOT NULL,
  parameters_hash text NOT NULL,
  algorithm_versions jsonb NOT NULL,
  grid_hash text NOT NULL,
  idempotency_key text NOT NULL,
  status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  outcome text CHECK (outcome IN ('completed', 'insufficient_observations', 'insufficient_coverage', 'coregistration_failed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  CHECK ((type = 'snapshot') = (period_b IS NULL)),
  CHECK ((outcome IS NULL) OR status = 'succeeded')
);
CREATE INDEX analysis_runs_project_ix ON analysis_runs (project_id);
CREATE INDEX analysis_runs_aoi_ix ON analysis_runs (aoi_id);
CREATE UNIQUE INDEX analysis_runs_idem_ux ON analysis_runs (idempotency_key)
  WHERE status IN ('queued', 'running', 'succeeded');

CREATE TABLE analysis_run_inputs (
  analysis_run_id uuid NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
  scene_id uuid NOT NULL REFERENCES scenes(id) ON DELETE RESTRICT,
  period text NOT NULL CHECK (period IN ('a', 'b')),
  datatake_id text,
  status text NOT NULL CHECK (status IN ('used', 'excluded')),
  reason_code text,
  CHECK ((status = 'excluded') = (reason_code IS NOT NULL)),
  PRIMARY KEY (analysis_run_id, scene_id)
);
CREATE INDEX analysis_run_inputs_scene_ix ON analysis_run_inputs (scene_id);

CREATE TABLE jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind text NOT NULL CHECK (kind IN ('analysis_run', 'analyst_turn', 'report_build')),
  analysis_run_id uuid REFERENCES analysis_runs(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  stage text,
  progress real NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 3,
  timeout_s int NOT NULL,
  idempotency_key text NOT NULL,
  payload jsonb NOT NULL,
  error jsonb,
  cancel_requested_at timestamptz,
  worker_id text,
  heartbeat_at timestamptz,
  not_before timestamptz NOT NULL DEFAULT now(),
  queued_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz
);
CREATE INDEX jobs_status_queued_ix ON jobs (status, queued_at);
CREATE INDEX jobs_analysis_run_ix ON jobs (analysis_run_id);

CREATE TABLE processing_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  analysis_run_id uuid REFERENCES analysis_runs(id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('aoi_measure', 'ingest', 'analysis')),
  algorithm_versions jsonb NOT NULL,
  parameters jsonb NOT NULL,
  parameters_hash text NOT NULL,
  grid_hash text,
  code_version text NOT NULL,
  environment jsonb NOT NULL,
  steps jsonb NOT NULL,
  timings jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz NOT NULL,
  completed_at timestamptz NOT NULL
);
CREATE INDEX processing_runs_analysis_ix ON processing_runs (analysis_run_id);

CREATE TABLE artifacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  sha256 text NOT NULL UNIQUE,
  uri text NOT NULL,
  media_type text NOT NULL,
  bytes bigint NOT NULL,
  kind text NOT NULL CHECK (kind IN ('window_cog', 'composite', 'n_valid', 'index', 'delta', 'z', 'category',
                                     'event_label', 'joint_valid', 'vector_gpkg', 'report_html', 'report_json')),
  crs_epsg int,
  transform double precision[],
  width int,
  height int,
  dtype text,
  nodata double precision,
  resolution_m double precision,
  band_names text[],
  produced_by_run_id uuid NOT NULL REFERENCES processing_runs(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (kind IN ('vector_gpkg', 'report_html', 'report_json') OR (
    crs_epsg IS NOT NULL AND transform IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL
    AND dtype IS NOT NULL AND resolution_m IS NOT NULL AND band_names IS NOT NULL))
);

CREATE TABLE evidence (
  evidence_id text PRIMARY KEY CHECK (evidence_id ~ '^ev_[0-9a-f]{32}$'),
  schema_version text NOT NULL,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  analysis_run_id uuid REFERENCES analysis_runs(id) ON DELETE CASCADE,
  processing_run_id uuid NOT NULL REFERENCES processing_runs(id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('aoi_measurement', 'catalog_query', 'scene_metadata', 'coverage_statistic',
                                     'index_statistic', 'coregistration_check', 'change_event', 'change_summary')),
  geom geometry(Geometry, 4326) CHECK (geom IS NULL OR ST_IsValid(geom)),
  geometry_artifact_id uuid REFERENCES artifacts(id),
  observed_period_a daterange,
  observed_period_b daterange,
  facts jsonb NOT NULL,
  confidence_band text NOT NULL CHECK (confidence_band IN ('higher', 'moderate', 'lower', 'n/a')),
  limitations jsonb NOT NULL,
  artifact_ids uuid[] NOT NULL DEFAULT '{}',
  content_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (analysis_run_id IS NOT NULL OR kind IN ('aoi_measurement', 'scene_metadata', 'catalog_query'))
);
CREATE INDEX evidence_geom_gix ON evidence USING gist (geom);
CREATE INDEX evidence_project_kind_ix ON evidence (project_id, kind);
CREATE INDEX evidence_analysis_ix ON evidence (analysis_run_id);
CREATE TRIGGER evidence_immutable BEFORE UPDATE OR DELETE ON evidence
  FOR EACH ROW EXECUTE FUNCTION aether_reject_mutation();

CREATE TABLE measurements (
  measurement_id text PRIMARY KEY CHECK (measurement_id ~ '^ms_[0-9a-f]{32}$'),
  evidence_id text NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
  quantity text NOT NULL,
  statistic text NOT NULL CHECK (statistic IN ('value', 'mean', 'median', 'p10', 'p90', 'count', 'sum', 'fraction')),
  status text NOT NULL CHECK (status IN ('available', 'unavailable')),
  value double precision,
  unavailable_reason text CHECK (unavailable_reason IN ('insufficient_observations', 'insufficient_coverage',
    'insufficient_support', 'coregistration_failed', 'robust_reference_unavailable', 'not_applicable')),
  unit text NOT NULL CHECK (unit IN ('m2', 'm', 'count', '1', 'deg', 'px')),
  denominator_quantity text,
  denominator_measurement_id text REFERENCES measurements(measurement_id) DEFERRABLE INITIALLY DEFERRED,
  n_samples bigint,
  region text NOT NULL CHECK (region IN ('aoi', 'event', 'none')),
  geometry_evidence_id text REFERENCES evidence(evidence_id),
  method text NOT NULL CHECK (method ~ '^[a-z0-9_.]+@[0-9a-z_.]+$'),
  parameters_hash text NOT NULL,
  processing_run_id uuid NOT NULL REFERENCES processing_runs(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((value IS NULL) = (status = 'unavailable')),
  CHECK (status <> 'unavailable' OR unavailable_reason IS NOT NULL),
  CHECK (status <> 'available' OR unavailable_reason IS NULL),
  CHECK (value IS NULL OR value NOT IN ('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8)),
  CHECK (statistic <> 'fraction' OR (denominator_quantity IS NOT NULL AND denominator_measurement_id IS NOT NULL AND unit = '1'))
);
CREATE INDEX measurements_evidence_ix ON measurements (evidence_id);
CREATE INDEX measurements_quantity_ix ON measurements (quantity);
CREATE TRIGGER measurements_immutable BEFORE UPDATE OR DELETE ON measurements
  FOR EACH ROW EXECUTE FUNCTION aether_reject_mutation();
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS measurements, evidence, artifacts, processing_runs, jobs, analysis_run_inputs,
  analysis_runs, assets, scenes, scene_searches, aois, projects CASCADE;
DROP FUNCTION IF EXISTS aether_reject_mutation();
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
