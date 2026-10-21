"""Prometheus exporter for a technocore-chat deployment (#766).

Reads the token-gated `/stats` digest and republishes the shared-storage aggregates as
Prometheus metrics. It consumes the published HTTP surface and ships nothing back into
the service, which is why it lives beside `mcp/` as its own package rather than in `src/`.
"""

from .collector import TechnocoreCollector
from .fetch import StatsUnavailableError, fetch_stats

VERSION = "0.1.0"

__all__ = ["TechnocoreCollector", "StatsUnavailableError", "fetch_stats", "VERSION"]
