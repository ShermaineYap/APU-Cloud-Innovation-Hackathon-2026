-- CampusPulse tables. BigQuery dialect; the local DuckDB backend transpiles this file.
-- `{ds}` is replaced with the dataset name (BigQuery) or dropped (DuckDB).

CREATE TABLE IF NOT EXISTS {ds}readings (
  ts            TIMESTAMP NOT NULL,   -- UTC, as reported by the node
  ts_local      DATETIME  NOT NULL,   -- Asia/Kuala_Lumpur wall clock
  weekday       INT64     NOT NULL,   -- 0 = Monday
  hour_local    INT64     NOT NULL,
  device_id     STRING    NOT NULL,
  room_id       STRING    NOT NULL,
  gateway_id    STRING,
  people        INT64,                -- estimated head count (simulated) or NULL
  occupied      BOOL,                 -- PIR motion in the last window
  co2_ppm       FLOAT64,
  temp_c        FLOAT64,
  humidity_pct  FLOAT64,
  watts         FLOAT64,
  ac_on         BOOL,
  rssi_dbm      FLOAT64,              -- link quality: received signal strength
  packets_sent  INT64,                -- reports attempted in the window
  packets_acked INT64,                -- reports confirmed by the broker (MQTT QoS 1)
  simulated     BOOL      NOT NULL
)
-- BigQuery only: PARTITION BY DATE(ts) CLUSTER BY room_id
;

CREATE TABLE IF NOT EXISTS {ds}rooms (
  room_id    STRING NOT NULL,
  name       STRING,
  block      STRING,
  floor      INT64,
  kind       STRING,
  capacity   INT64,
  ac_kw      FLOAT64,
  gateway_id STRING,
  col        INT64,
  row        INT64
);

CREATE TABLE IF NOT EXISTS {ds}timetable (
  room_id    STRING NOT NULL,
  weekday    INT64,
  start_hour INT64,
  end_hour   INT64,
  module     STRING,
  expected   INT64
);

CREATE TABLE IF NOT EXISTS {ds}decisions (
  decided_at   TIMESTAMP NOT NULL,
  action_id    STRING    NOT NULL,
  kind         STRING,
  title        STRING,
  status       STRING,                -- approved | dismissed
  kwh_per_week FLOAT64,
  rm_per_week  FLOAT64,
  decided_by   STRING
);
