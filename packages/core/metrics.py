"""Loopback-only Prometheus / OpenMetrics telemetry registry.

Collects in-process counters and latency histograms for HTTP requests, worker
queue tasks, crawler responses, and hallucination-guard checks without
exporting any data over the network (CLAUDE.md §1, §11).
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import defaultdict
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

HTTP_DURATION_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

WORKER_DURATION_BUCKETS: tuple[float, ...] = (
    0.1,
    0.5,
    1.0,
    5.0,
    15.0,
    60.0,
    300.0,
    900.0,
    1800.0,
)

_UUID_SEGMENT_RE = re.compile(
    r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?=/|$)"
)
_INT_SEGMENT_RE = re.compile(r"/\d+(?=/|$)")


def normalize_route_path(path: str) -> str:
    """Collapse dynamic UUID and integer path segments to `{id}`."""
    if not path:
        return "/"
    normalized = _UUID_SEGMENT_RE.sub("/{id}", path)
    normalized = _INT_SEGMENT_RE.sub("/{id}", normalized)
    return normalized


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels)
    return f"{{{inner}}}"


class _HistogramState:
    """Cumulative bucket counts, sum, and sample count for one label set."""

    __slots__ = ("buckets", "counts", "sum_value", "total_count")

    def __init__(self, buckets: tuple[float, ...]) -> None:
        self.buckets = buckets
        self.counts = [0] * len(buckets)
        self.sum_value = 0.0
        self.total_count = 0

    def observe(self, value: float) -> None:
        v = max(0.0, float(value))
        if not math.isfinite(v):
            return
        self.sum_value += v
        self.total_count += 1
        for idx, bound in enumerate(self.buckets):
            if v <= bound:
                self.counts[idx] += 1


class MetricsRegistry:
    """Thread-safe in-process metrics store with Prometheus text rendering."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._http_requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._http_durations: dict[tuple[str, str], _HistogramState] = {}
        self._worker_tasks: dict[tuple[str, str], int] = defaultdict(int)
        self._worker_durations: dict[str, _HistogramState] = {}
        self._crawler_responses: dict[tuple[str, str], int] = defaultdict(int)
        self._guard_verifications: dict[str, int] = defaultdict(int)

    def reset(self) -> None:
        """Clear all recorded metrics (used in tests)."""
        with self._lock:
            self._http_requests.clear()
            self._http_durations.clear()
            self._worker_tasks.clear()
            self._worker_durations.clear()
            self._crawler_responses.clear()
            self._guard_verifications.clear()

    def record_http_request(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_s: float,
    ) -> None:
        m = method.upper()
        r = normalize_route_path(route)
        s = str(int(status_code))
        with self._lock:
            self._http_requests[(m, r, s)] += 1
            hist = self._http_durations.get((m, r))
            if hist is None:
                hist = _HistogramState(HTTP_DURATION_BUCKETS)
                self._http_durations[(m, r)] = hist
            hist.observe(duration_s)

    def record_worker_task(
        self,
        *,
        kind: str,
        outcome: str,
        duration_s: float,
    ) -> None:
        k = str(kind)
        o = str(outcome)
        with self._lock:
            self._worker_tasks[(k, o)] += 1
            hist = self._worker_durations.get(k)
            if hist is None:
                hist = _HistogramState(WORKER_DURATION_BUCKETS)
                self._worker_durations[k] = hist
            hist.observe(duration_s)

    def record_crawler_response(self, *, vendor: str, status_code: int) -> None:
        with self._lock:
            self._crawler_responses[(str(vendor), str(int(status_code)))] += 1

    def record_guard_verification(self, *, passed: bool) -> None:
        outcome = "passed" if passed else "refused"
        with self._lock:
            self._guard_verifications[outcome] += 1

    def snapshot(self) -> dict[str, Any]:
        """Return a structured dictionary snapshot of current metric counters."""
        with self._lock:
            return {
                "http_requests_total": {
                    f"{m} {r} {s}": count
                    for (m, r, s), count in sorted(self._http_requests.items())
                },
                "worker_tasks_total": {
                    f"{k}:{o}": count for (k, o), count in sorted(self._worker_tasks.items())
                },
                "crawler_http_responses_total": {
                    f"{v}:{s}": count for (v, s), count in sorted(self._crawler_responses.items())
                },
                "guard_verifications_total": dict(sorted(self._guard_verifications.items())),
            }

    def render_prometheus(self) -> str:
        """Format all registered metrics in Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            lines.append(
                "# HELP jobrunner_http_requests_total Total HTTP requests handled by the API."
            )
            lines.append("# TYPE jobrunner_http_requests_total counter")
            for (method, route, status), count in sorted(self._http_requests.items()):
                lbl = _format_labels((("method", method), ("route", route), ("status", status)))
                lines.append(f"jobrunner_http_requests_total{lbl} {count}")

            lines.append(
                "# HELP jobrunner_http_request_duration_seconds HTTP request latency in seconds."
            )
            lines.append("# TYPE jobrunner_http_request_duration_seconds histogram")
            for (method, route), hist in sorted(self._http_durations.items()):
                base_labels = (("method", method), ("route", route))
                for bound, bucket_count in zip(hist.buckets, hist.counts, strict=True):
                    lbl = _format_labels((*base_labels, ("le", f"{bound:g}")))
                    lines.append(
                        f"jobrunner_http_request_duration_seconds_bucket{lbl} {bucket_count}"
                    )
                inf_lbl = _format_labels((*base_labels, ("le", "+Inf")))
                lines.append(
                    f"jobrunner_http_request_duration_seconds_bucket{inf_lbl} {hist.total_count}"
                )
                sum_lbl = _format_labels(base_labels)
                lines.append(
                    f"jobrunner_http_request_duration_seconds_sum{sum_lbl} {hist.sum_value:.6f}"
                )
                lines.append(
                    f"jobrunner_http_request_duration_seconds_count{sum_lbl} {hist.total_count}"
                )

            lines.append(
                "# HELP jobrunner_worker_tasks_total Total background worker tasks processed."
            )
            lines.append("# TYPE jobrunner_worker_tasks_total counter")
            for (kind, outcome), count in sorted(self._worker_tasks.items()):
                lbl = _format_labels((("kind", kind), ("outcome", outcome)))
                lines.append(f"jobrunner_worker_tasks_total{lbl} {count}")

            lines.append(
                "# HELP jobrunner_worker_task_duration_seconds Worker task duration in seconds."
            )
            lines.append("# TYPE jobrunner_worker_task_duration_seconds histogram")
            for kind, hist in sorted(self._worker_durations.items()):
                worker_labels = (("kind", kind),)
                for bound, bucket_count in zip(hist.buckets, hist.counts, strict=True):
                    lbl = _format_labels((*worker_labels, ("le", f"{bound:g}")))
                    lines.append(
                        f"jobrunner_worker_task_duration_seconds_bucket{lbl} {bucket_count}"
                    )
                inf_lbl = _format_labels((*worker_labels, ("le", "+Inf")))
                lines.append(
                    f"jobrunner_worker_task_duration_seconds_bucket{inf_lbl} {hist.total_count}"
                )
                sum_lbl = _format_labels(worker_labels)
                lines.append(
                    f"jobrunner_worker_task_duration_seconds_sum{sum_lbl} {hist.sum_value:.6f}"
                )
                lines.append(
                    f"jobrunner_worker_task_duration_seconds_count{sum_lbl} {hist.total_count}"
                )

            lines.append(
                "# HELP jobrunner_crawler_http_responses_total Crawler HTTP responses by vendor."
            )
            lines.append("# TYPE jobrunner_crawler_http_responses_total counter")
            for (vendor, status), count in sorted(self._crawler_responses.items()):
                lbl = _format_labels((("vendor", vendor), ("status", status)))
                lines.append(f"jobrunner_crawler_http_responses_total{lbl} {count}")

            lines.append("# HELP jobrunner_guard_verifications_total Hallucination guard outcomes.")
            lines.append("# TYPE jobrunner_guard_verifications_total counter")
            for outcome, count in sorted(self._guard_verifications.items()):
                lbl = _format_labels((("outcome", outcome),))
                lines.append(f"jobrunner_guard_verifications_total{lbl} {count}")

        return "\n".join(lines) + "\n"


_REGISTRY = MetricsRegistry()


def get_metrics_registry() -> MetricsRegistry:
    """Return the process-wide metrics registry."""
    return _REGISTRY


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record request count and latency for every HTTP request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        route_obj = request.scope.get("route")
        route_path = getattr(route_obj, "path", None) or request.url.path
        _REGISTRY.record_http_request(
            method=request.method,
            route=str(route_path),
            status_code=response.status_code,
            duration_s=elapsed,
        )
        return response
