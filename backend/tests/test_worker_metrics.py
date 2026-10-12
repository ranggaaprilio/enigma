"""Epic 09: a worker serves its own /metrics, gated like the API's and the proxy's."""

from __future__ import annotations

import socket

import httpx

from app.worker_metrics import WorkerMetricsServer, start_if_possible
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


def test_a_stalled_client_does_not_block_the_next_scrape() -> None:
    server = _serve(metrics_token=None)
    stalled = socket.create_connection(("127.0.0.1", server.port))
    try:
        # Connected, never sends a request line: the listener must still answer others.
        assert httpx.get(f"http://127.0.0.1:{server.port}/metrics", timeout=5).status_code == 200
    finally:
        stalled.close()
        server.stop()


def test_a_taken_port_costs_the_worker_its_metrics_not_its_startup() -> None:
    holder = socket.socket()
    holder.bind(("0.0.0.0", 0))
    holder.listen()
    try:
        taken = holder.getsockname()[1]
        assert start_if_possible(queue_settings(metrics_port=taken)) is None
    finally:
        holder.close()
