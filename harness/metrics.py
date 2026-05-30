from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class EvalThresholds:
    routing_accuracy_min: float = 0.90
    rag_keyword_hit_min: float = 0.75
    avg_latency_ms_max: float = 3000.0


def safe_div(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def calc_accuracy(correct: int, total: int) -> float:
    return safe_div(correct, total)


def calc_keyword_hit_rate(hit_count: int, total: int) -> float:
    return safe_div(hit_count, total)


def calc_avg_latency(latencies_ms: list[float]) -> float:
    if not latencies_ms:
        return 0.0
    return sum(latencies_ms) / len(latencies_ms)


def check_thresholds(
    routing_accuracy: float,
    rag_keyword_hit: float,
    avg_latency_ms: float,
    thresholds: EvalThresholds,
) -> dict[str, Any]:
    routing_passed = routing_accuracy >= thresholds.routing_accuracy_min
    rag_passed = rag_keyword_hit >= thresholds.rag_keyword_hit_min
    latency_passed = avg_latency_ms <= thresholds.avg_latency_ms_max
    passed = routing_passed and rag_passed and latency_passed
    return {
        "passed": passed,
        "routing_passed": routing_passed,
        "rag_passed": rag_passed,
        "latency_passed": latency_passed,
    }
