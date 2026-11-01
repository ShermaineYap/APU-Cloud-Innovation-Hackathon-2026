-- Curated views. These are the only relations the Gemini SQL agent may query.

-- One row per room per local hour.
CREATE OR REPLACE VIEW {ds}v_room_hourly AS
SELECT
  r.room_id,
  rm.name AS room_name,
  rm.block,
  rm.kind,
  rm.capacity,
  DATETIME_TRUNC(r.ts_local, HOUR) AS hour,
  ANY_VALUE(r.weekday) AS weekday,
  ANY_VALUE(r.hour_local) AS hour_of_day,
  COUNT(*) AS reports,
  AVG(r.people) AS avg_people,
  MAX(r.people) AS max_people,
  AVG(IF(r.occupied, 1.0, 0.0)) AS occupied_frac,
  AVG(r.co2_ppm) AS avg_co2_ppm,
  MAX(r.co2_ppm) AS max_co2_ppm,
  AVG(r.temp_c) AS avg_temp_c,
  AVG(IF(r.ac_on, 1.0, 0.0)) AS ac_on_frac,
  AVG(r.watts) / 1000.0 AS kwh,  -- mean kW over one hour = kWh for that hour
  SAFE_DIVIDE(AVG(r.people), ANY_VALUE(rm.capacity)) AS utilisation,
  LOGICAL_AND(r.simulated) AS simulated
FROM {ds}readings AS r
JOIN {ds}rooms AS rm USING (room_id)
GROUP BY r.room_id, rm.name, rm.block, rm.kind, rm.capacity, DATETIME_TRUNC(r.ts_local, HOUR);

-- Hours where a room was cooled with nobody in it.
CREATE OR REPLACE VIEW {ds}v_empty_cooled AS
SELECT room_id, room_name, block, hour, weekday, hour_of_day, kwh, ac_on_frac
FROM {ds}v_room_hourly
WHERE ac_on_frac >= 0.5 AND COALESCE(max_people, 0) = 0 AND occupied_frac = 0;

-- Link health per device per local day.
CREATE OR REPLACE VIEW {ds}v_network_daily AS
SELECT
  device_id,
  room_id,
  gateway_id,
  DATETIME_TRUNC(ts_local, DAY) AS day,
  COUNT(*) AS reports,
  SUM(packets_sent) AS packets_sent,
  SUM(packets_acked) AS packets_acked,
  SAFE_DIVIDE(SUM(packets_acked), SUM(packets_sent)) AS delivery_ratio,
  AVG(rssi_dbm) AS avg_rssi_dbm,
  MIN(rssi_dbm) AS min_rssi_dbm,
  MAX(ts) AS last_seen
FROM {ds}readings
GROUP BY device_id, room_id, gateway_id, DATETIME_TRUNC(ts_local, DAY);

-- Booked slots joined to what actually happened in the room.
CREATE OR REPLACE VIEW {ds}v_slot_attendance AS
SELECT
  h.room_id,
  h.room_name,
  h.block,
  h.capacity,
  t.module,
  t.weekday,
  t.start_hour,
  t.expected,
  DATETIME_TRUNC(h.hour, DAY) AS day,
  AVG(h.avg_people) AS avg_people,
  MAX(h.max_people) AS max_people,
  AVG(h.max_co2_ppm) AS avg_peak_co2_ppm,
  SUM(h.kwh) AS kwh
FROM {ds}v_room_hourly AS h
JOIN {ds}timetable AS t
  ON t.room_id = h.room_id
 AND t.weekday = h.weekday
 AND h.hour_of_day >= t.start_hour
 AND h.hour_of_day < t.end_hour
GROUP BY h.room_id, h.room_name, h.block, h.capacity, t.module, t.weekday, t.start_hour, t.expected,
         DATETIME_TRUNC(h.hour, DAY);
