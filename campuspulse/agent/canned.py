"""Tested questions: shown as suggestions in the app and used in offline mode.

Each SQL here has been run against the demo data. Keep at least these working
for the demo video, whatever Gemini does with unscripted questions.
"""

WEEK = "hour >= DATETIME_SUB((SELECT MAX(hour) FROM v_room_hourly), INTERVAL 7 DAY)"

CANNED: list[dict] = [
    {"q": "Which rooms wasted the most energy cooling while empty this week?",
     "keys": ["wast", "empty", "cool"],
     "sql": f"""SELECT room_id, room_name, COUNT(*) AS empty_cooled_hours, ROUND(SUM(kwh), 1) AS kwh
FROM v_empty_cooled WHERE {WEEK}
GROUP BY room_id, room_name ORDER BY kwh DESC LIMIT 10"""},
    {"q": "Which rooms went above 1000 ppm CO2 while occupied this week?",
     "keys": ["co2", "stale", "air", "1000"],
     "sql": f"""SELECT room_id, room_name, COUNT(*) AS hours_over_limit, ROUND(MAX(max_co2_ppm)) AS worst_ppm
FROM v_room_hourly WHERE {WEEK} AND max_co2_ppm >= 1000 AND occupied_frac > 0
GROUP BY room_id, room_name ORDER BY hours_over_limit DESC"""},
    {"q": "Which classes use less than a third of their room's seats?",
     "keys": ["third", "seats", "under", "half empty", "too big"],
     "sql": """SELECT module, room_id, capacity, ROUND(AVG(avg_people)) AS avg_people,
       ROUND(AVG(avg_people) / ANY_VALUE(capacity), 2) AS seat_use
FROM v_slot_attendance GROUP BY module, room_id, capacity
HAVING AVG(avg_people) / ANY_VALUE(capacity) < 0.34 ORDER BY seat_use"""},
    {"q": "Which classes have more students than seats?",
     "keys": ["more students", "oversubscribed", "overcrowd", "than seats"],
     "sql": """SELECT module, room_id, capacity, MAX(expected) AS timetabled, MAX(max_people) AS peak_people
FROM v_slot_attendance GROUP BY module, room_id, capacity
HAVING MAX(max_people) > capacity OR MAX(expected) > capacity ORDER BY peak_people DESC"""},
    {"q": "How much energy did each block use this week?",
     "keys": ["each block", "per block", "block use"],
     "sql": f"""SELECT block, ROUND(SUM(kwh)) AS kwh, ROUND(SUM(kwh) * 0.5) AS approx_rm
FROM v_room_hourly WHERE {WEEK} GROUP BY block ORDER BY block"""},
    {"q": "What does a typical weekday look like for occupancy in Block A?",
     "keys": ["typical", "weekday", "profile"],
     "sql": """SELECT hour_of_day, ROUND(AVG(avg_people), 1) AS avg_people, ROUND(AVG(ac_on_frac), 2) AS ac_on
FROM v_room_hourly WHERE block = 'A' AND weekday < 5 GROUP BY hour_of_day ORDER BY hour_of_day"""},
    {"q": "Which sensor nodes have the weakest network link?",
     "keys": ["weak", "network", "link", "signal", "rssi"],
     "sql": """SELECT device_id, room_id, gateway_id, ROUND(AVG(delivery_ratio), 3) AS delivery,
       ROUND(AVG(avg_rssi_dbm), 1) AS rssi_dbm
FROM v_network_daily GROUP BY device_id, room_id, gateway_id ORDER BY delivery LIMIT 8"""},
    {"q": "When did Auditorium 2 run its AC on the weekend?",
     "keys": ["weekend", "auditorium 2", "a-02"],
     "sql": """SELECT hour, ROUND(kwh, 2) AS kwh FROM v_empty_cooled
WHERE room_id = 'A-02' AND weekday >= 5 ORDER BY hour DESC LIMIT 60"""},
    {"q": "How full is each room on average when it is in use?",
     "keys": ["how full", "utilisation", "utilization", "average use"],
     "sql": """SELECT room_id, room_name, capacity, ROUND(AVG(utilisation), 2) AS avg_utilisation
FROM v_room_hourly WHERE occupied_frac > 0 GROUP BY room_id, room_name, capacity ORDER BY avg_utilisation"""},
    {"q": "Is attendance lower on Friday afternoons?",
     "keys": ["friday"],
     "sql": """SELECT CASE weekday WHEN 4 THEN 'Friday' ELSE 'Mon-Thu' END AS days,
       ROUND(AVG(avg_people / expected), 2) AS attendance_rate
FROM v_slot_attendance WHERE start_hour >= 13 AND block = 'A' GROUP BY days"""},
    {"q": "What was the hottest room this week?",
     "keys": ["hottest", "temperature", "warm"],
     "sql": f"""SELECT room_id, room_name, ROUND(MAX(avg_temp_c), 1) AS max_temp_c
FROM v_room_hourly WHERE {WEEK} AND occupied_frac > 0 GROUP BY room_id, room_name ORDER BY max_temp_c DESC LIMIT 5"""},
    {"q": "Show daily energy use for the whole campus over the last four weeks",
     "keys": ["daily energy", "four weeks", "trend", "over time"],
     "sql": """SELECT DATETIME_TRUNC(hour, DAY) AS day, ROUND(SUM(kwh)) AS kwh
FROM v_room_hourly
WHERE hour > DATETIME_TRUNC((SELECT MIN(hour) FROM v_room_hourly), DAY) + INTERVAL 1 DAY
  AND hour < DATETIME_TRUNC((SELECT MAX(hour) FROM v_room_hourly), DAY)
GROUP BY day ORDER BY day"""},
    {"q": "Which gateway had the most silent hours?",
     "keys": ["gateway", "silent", "outage"],
     "sql": """SELECT gateway_id, day, SUM(reports) AS reports FROM v_network_daily
WHERE day > (SELECT MIN(day) FROM v_network_daily) AND day < (SELECT MAX(day) FROM v_network_daily)
GROUP BY gateway_id, day ORDER BY reports LIMIT 6"""},
    {"q": "Which rooms are free on Wednesday at 2 pm?",
     "keys": ["free", "available", "wednesday"],
     "sql": """SELECT r.room_id, r.name, r.capacity FROM rooms AS r
WHERE r.room_id NOT IN (SELECT room_id FROM timetable WHERE weekday = 2 AND start_hour <= 14 AND end_hour > 14)
  AND r.kind != 'study' ORDER BY r.capacity"""},
    {"q": "How much could we save in a year if we stopped cooling empty rooms?",
     "keys": ["year", "annual", "save"],
     "sql": f"""SELECT ROUND(SUM(kwh)) AS kwh_last_week, ROUND(SUM(kwh) * 52) AS kwh_per_year,
       ROUND(SUM(kwh) * 52 * 0.5) AS approx_rm_per_year
FROM v_empty_cooled WHERE {WEEK}"""},
]


def match(question: str) -> dict | None:
    q = question.lower().strip()
    for c in CANNED:
        if q == c["q"].lower():
            return c
    best, hits = None, 0
    for c in CANNED:
        n = sum(k in q for k in c["keys"])
        if n > hits:
            best, hits = c, n
    return best
