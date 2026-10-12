"""Epic 09: a worker serves its own /metrics, gated like the API's and the proxy's."""

from __future__ import annotations

import httpx

from app.worker_metrics import WorkerMetricsServer
from tests.test_job_queue import queue_settings


def _serve(**overrides):
    return WorkerMetricsServer(queue_settings(**overrides), host="127.0.0.1", port=0).start()


def test_worker_metrics_require_the_bearer_token_when_one_is_set() -> None:
    server = _serve(metrics_token="scrape-me")
    try:
        base = f"http://127.0.0.1:{server.port}"
        anonymous = httpx.get(f"{base}/metrics")
        assert anonymous.status_code == 401
        assert anonymous.headers["WWW-Authenticate"] == "Bearer"
        wrong = httpx.get(f"{base}/metrics", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401

        scrape = httpx.get(f"{base}/metrics", headers={"Authorization": "Bearer scrape-me"})
        assert scrape.status_code == 200
        assert scrape.headers["content-type"].startswith("text/plain")
        # Worker-side series (runs, reaper, lease) and this process's pool are all here.
        for name in ("runs_finished_total", "reaper_sweeps_total", "run_lease_lost_total"):
            assert f"# HELP {name}" in scrape.text
        assert "db_pool_size" in scrape.text
        other = httpx.get(f"{base}/other", headers={"Authorization": "Bearer scrape-me"})
        assert other.status_code == 404
    finally:
        server.stop()


def test_worker_metrics_are_open_without_a_token_in_local_development() -> None:
    server = _serve(metrics_token=None)
    try:
        assert httpx.get(f"http://127.0.0.1:{server.port}/metrics").status_code == 200
    finally:
        server.stop()
