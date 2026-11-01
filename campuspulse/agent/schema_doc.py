"""The data dictionary Gemini sees. Keep it in step with bigquery/views.sql."""

SCHEMA_DOC = """
You can query these BigQuery tables and views (BigQuery Standard SQL, bare names, no dataset prefix).
Times in *_local, hour and day columns are Malaysia time (Asia/Kuala_Lumpur), type DATETIME.
weekday is 0 = Monday ... 6 = Sunday.

v_room_hourly - one row per room per hour
  room_id, room_name, block ('A','B','C'), kind ('lecture','lab','tutorial','study'), capacity,
  hour DATETIME, weekday, hour_of_day, reports, avg_people, max_people,
  occupied_frac (0-1, share of the hour the PIR saw motion), avg_co2_ppm, max_co2_ppm, avg_temp_c,
  ac_on_frac (0-1), kwh (energy used in that hour), utilisation (avg_people / capacity), simulated

v_empty_cooled - hours where AC ran for at least half the hour with nobody in the room
  room_id, room_name, block, hour, weekday, hour_of_day, kwh, ac_on_frac

v_slot_attendance - each timetabled class session joined to what the sensors saw
  room_id, room_name, block, capacity, module, weekday, start_hour, expected (students timetabled),
  day DATETIME, avg_people, max_people, avg_peak_co2_ppm, kwh

v_network_daily - link health per sensor node per day
  device_id, room_id, gateway_id, day, reports, packets_sent, packets_acked,
  delivery_ratio (0-1), avg_rssi_dbm, min_rssi_dbm, last_seen TIMESTAMP

rooms - room_id, name, block, floor, kind, capacity, ac_kw, gateway_id
timetable - room_id, weekday, start_hour, end_hour, module, expected
readings - raw 5-minute (simulated) or 30-second (live node) reports:
  ts TIMESTAMP, ts_local, weekday, hour_local, device_id, room_id, gateway_id, people, occupied,
  co2_ppm, temp_c, humidity_pct, watts, ac_on, rssi_dbm, packets_sent, packets_acked, simulated

Useful facts:
  * "This week" or "last 7 days": hour >= DATETIME_SUB((SELECT MAX(hour) FROM v_room_hourly), INTERVAL 7 DAY)
  * CO2 above 1000 ppm counts as stale air. Energy price is RM {tariff} per kWh (an assumption).
  * Prefer the v_ views; use readings only for minute-level questions.
"""
