-- BigQuery ML. Run with `python scripts/bq_setup.py --ml` after data is loaded.
-- Local mode computes equivalent baselines in pandas (campuspulse/analytics.py).

-- 1. Occupancy forecast: one ARIMA_PLUS series per room, hourly, weekly seasonality.
CREATE OR REPLACE MODEL {ds}occupancy_forecast
OPTIONS (
  model_type = 'ARIMA_PLUS',
  time_series_timestamp_col = 'hour',
  time_series_data_col = 'avg_people',
  time_series_id_col = 'room_id',
  data_frequency = 'HOURLY',
  holiday_region = 'MY',
  auto_arima = TRUE
) AS
SELECT room_id, CAST(hour AS TIMESTAMP) AS hour, COALESCE(avg_people, 0) AS avg_people
FROM {ds}v_room_hourly;

-- 2. Energy model used for anomaly detection on hourly kWh.
CREATE OR REPLACE MODEL {ds}energy_model
OPTIONS (
  model_type = 'ARIMA_PLUS',
  time_series_timestamp_col = 'hour',
  time_series_data_col = 'kwh',
  time_series_id_col = 'room_id',
  data_frequency = 'HOURLY',
  decompose_time_series = TRUE
) AS
SELECT room_id, CAST(hour AS TIMESTAMP) AS hour, kwh
FROM {ds}v_room_hourly;

-- Example reads the app issues:
-- SELECT * FROM ML.FORECAST(MODEL {ds}occupancy_forecast, STRUCT(24 AS horizon, 0.9 AS confidence_level));
-- SELECT * FROM ML.DETECT_ANOMALIES(MODEL {ds}energy_model, STRUCT(0.99 AS anomaly_prob_threshold))
--   WHERE is_anomaly;
