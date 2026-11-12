"""Check a CampusPulse deployment end to end and print PASS/FAIL for each part.

    python scripts/cloud_check.py                 # uses .deploy.env written by deploy/deploy.sh
    python scripts/cloud_check.py --url https://campuspulse-xxxx.run.app --project my-project

Part 1 runs every query the app makes against your real BigQuery dataset (needs
`gcloud auth application-default login`). Part 2 calls the deployed Cloud Run service:
brief, room drawer with the BigQuery ML forecast, Ask (Gemini), report, ingest.
Nothing here prints keys.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

results: list[tuple[str, bool, str]] = []


def check(name: str):
    def wrap(fn):
        t = time.time()
        try:
            detail = fn() or ""
            results.append((name, True, f"{detail} ({time.time() - t:.1f}s)"))
            print(f"PASS  {name}  {detail}")
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}"))
            print(f"FAIL  {name}  {type(e).__name__}: {str(e)[:300]}")
            if os.getenv("CHECK_DEBUG"):
                traceback.print_exc()
        return fn
    return wrap


def load_deploy_env() -> dict:
    env = {}
    p = ROOT / ".deploy.env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def bigquery_checks(project: str, dataset: str, location: str) -> None:
    os.environ.update(CP_BACKEND="bigquery", GOOGLE_CLOUD_PROJECT=project, CP_BQ_DATASET=dataset,
                      CP_BQ_LOCATION=location)
    os.environ.pop("GEMINI_API_KEY", None)  # Part 1 tests the warehouse only
    from campuspulse import analytics, decisions, network
    from campuspulse.agent.canned import CANNED
    from campuspulse.agent.sqlguard import guard
    from campuspulse.store.bq import BigQueryStore

    store = BigQueryStore()
    print(f"\nPart 1: BigQuery dataset {store.ds}")

    @check("readings loaded")
    def _():
        n = store.reading_count()
        assert n > 1000, f"only {n} rows; run deploy/deploy.sh (it seeds history)"
        return f"{n:,} rows"

    @check("rooms and timetable tables")
    def _():
        r = store.query("SELECT COUNT(*) AS n FROM rooms")["n"].iloc[0]
        t = store.query("SELECT COUNT(*) AS n FROM timetable")["n"].iloc[0]
        assert r == 20 and t > 200, f"rooms={r}, timetable={t}"
        return f"{r} rooms, {t} classes"

    @check("KPIs (v_room_hourly)")
    def _():
        k = analytics.kpis(store)
        return f"{k['wasted_kwh']:,.0f} kWh cooled empty, {k['wasted_share']:.0%} of cooling"

    @check("floor plan now (partition-pruned)")
    def _():
        states = analytics.room_states(store)
        live = sum(s["status"] != "offline" for s in states)
        assert live >= 15, f"only {live} rooms reporting: is the service catching up readings?"
        return f"{live}/20 rooms reporting"

    @check("floor plan replay (?at=)")
    def _():
        import pandas as pd
        r = analytics.replay_range(store)
        at = pd.Timestamp(r["last"]).normalize() - pd.Timedelta(days=1) + pd.Timedelta(hours=11)
        states = analytics.room_states(store, at=at)
        return f"{sum(s['status'] != 'offline' for s in states)}/20 rooms at {at:%a %H:%M}"

    @check("network confidence and gateway outages")
    def _():
        h = network.link_health(store)
        o = network.gateway_outages(store)
        return f"{len(h)} nodes, {o['gateway_id'].nunique()} gateway(s) with silent hours"

    @check("decision engine (all detectors)")
    def _():
        acts = decisions.build_actions(store)
        assert acts, "no actions"
        return f"{len(acts)} actions; top: {acts[0]['title'][:60]}"

    @check("latest decision per action (QUALIFY)")
    def _():
        store.query("""SELECT action_id, status FROM decisions WHERE TRUE
            QUALIFY ROW_NUMBER() OVER (PARTITION BY action_id ORDER BY decided_at DESC) = 1""")

    @check("BigQuery ML forecast in the room drawer")
    def _():
        d = analytics.room_detail(store, "AUD-1")
        assert d["forecast_method"].startswith("BigQuery ML"), (
            "fell back to the local baseline: train the models with `python scripts/bq_setup.py --ml`")
        return d["forecast_method"]

    @check("BigQuery ML anomaly detection")
    def _():
        d = analytics.room_detail(store, "AUD-2")
        assert d["anomaly_method"].startswith("BigQuery ML"), "fell back to the local z-score"
        return f"{len(d['anomalies'])} unusual hours in AUD-2"

    @check("all 15 tested Ask questions run on BigQuery")
    def _():
        bad = []
        for c in CANNED:
            try:
                store.query(guard(c["sql"]))
            except Exception as e:
                bad.append(f"{c['q'][:40]}: {str(e)[:80]}")
        assert not bad, "; ".join(bad)
        return f"{len(CANNED)} questions"


def service_checks(url: str, key: str | None) -> None:
    import httpx

    url = url.rstrip("/")
    http = httpx.Client(timeout=90)
    print(f"\nPart 2: Cloud Run service {url}")

    @check("service up (cold start included)")
    def _():
        h = http.get(f"{url}/api/health").json()
        assert h["backend"] == "bigquery", f"backend is {h['backend']}"
        return f"backend {h['backend']}, Gemini {'on' if h['gemini'] else 'OFF'} ({h['model']})"

    @check("dashboard page")
    def _():
        r = http.get(url)
        assert r.status_code == 200 and "CampusPulse" in r.text

    @check("overview: rooms reporting now")
    def _():
        o = http.get(f"{url}/api/overview").json()
        live = sum(r["status"] != "offline" for r in o["rooms"])
        assert live >= 15, f"only {live} rooms reporting"
        return f"{live}/20 rooms, study finder: {o['study']['message'][:50]}"

    @check("Gemini brief")
    def _():
        b = http.get(f"{url}/api/brief", params={"refresh": "true"}).json()["brief"]
        assert b["source"].startswith("Gemini"), b["source"]
        return b["headline"][:70]

    @check("Gemini chart caption")
    def _():
        a = http.get(f"{url}/api/actions").json()[0]
        c = http.get(f"{url}/api/actions/{a['id']}/caption").json()
        assert c["source"].startswith("Gemini"), c["source"]
        return c["caption"][:70]

    @check("Ask the data (Gemini writes SQL, BigQuery runs it)")
    def _():
        a = http.post(f"{url}/api/ask", json={"question": "Which three rooms used the most energy yesterday?"}).json()
        assert a["source"].startswith("Gemini"), a["source"]
        assert a.get("sql"), "no SQL"
        return a["answer"][:70]

    @check("room drawer uses BigQuery ML")
    def _():
        d = http.get(f"{url}/api/rooms/AUD-1").json()
        assert d["forecast_method"].startswith("BigQuery ML"), d["forecast_method"]
        return d["forecast_method"]

    @check("Estates report PDF")
    def _():
        r = http.get(f"{url}/api/report.pdf")
        assert r.status_code == 200 and r.content[:5] == b"%PDF-"
        return f"{len(r.content) // 1024} KB"

    if key:
        @check("ingest a reading (via Pub/Sub when configured)")
        def _():
            r = http.post(f"{url}/api/ingest", headers={"X-Device-Key": key},
                          json={"device_id": "cloud-check", "room_id": "B-08-05", "occupied": True,
                                "co2_ppm": 650, "temp_c": 24.0, "rssi_dbm": -60})
            assert r.status_code == 200, r.text[:200]
            return f"accepted via {r.json()['via']}"
    else:
        print("SKIP  ingest (no INGEST_KEY in .deploy.env or --key)")


def main() -> int:
    env = load_deploy_env()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT") or env.get("PROJECT_ID"))
    ap.add_argument("--dataset", default=os.getenv("CP_BQ_DATASET", "campuspulse"))
    ap.add_argument("--location", default=os.getenv("CP_BQ_LOCATION", "asia-southeast1"))
    ap.add_argument("--url", default=env.get("URL"))
    ap.add_argument("--key", default=env.get("INGEST_KEY"))
    ap.add_argument("--skip-bigquery", action="store_true")
    args = ap.parse_args()
    if not args.project:
        try:
            import subprocess
            args.project = subprocess.run(["gcloud", "config", "get-value", "project"], capture_output=True,
                                          text=True).stdout.strip() or None
        except FileNotFoundError:
            pass
    if not args.skip_bigquery:
        if not args.project:
            sys.exit("Give --project (or run `gcloud config set project ...`).")
        bigquery_checks(args.project, args.dataset, args.location)
    if args.url:
        service_checks(args.url, args.key)
    else:
        print("\nSKIP  Part 2: no --url and no URL in .deploy.env")
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
