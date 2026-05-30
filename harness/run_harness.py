from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from metrics import (
    EvalThresholds,
    calc_accuracy,
    calc_avg_latency,
    calc_keyword_hit_rate,
    check_thresholds,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
HARNESS_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = HARNESS_DIR / "config.yaml"
DEFAULT_ROUTING_DATASET = HARNESS_DIR / "datasets" / "routing_v1.jsonl"
DEFAULT_RAG_DATASET = HARNESS_DIR / "datasets" / "rag_v1.jsonl"
DEFAULT_RESULTS_DIR = HARNESS_DIR / "results"
DEFAULT_REPORT_TEMPLATE = HARNESS_DIR / "templates" / "report_template.md"


def parse_simple_yaml(path: Path) -> dict[str, Any]:
    """
    解析最小YAML子集，仅支持：
    key: value
    nested:
      child: value
    list:
      - item
    """
    if not path.exists():
        return {}

    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(0, root)]

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        while stack and indent < stack[-1][0]:
            stack.pop()
        container = stack[-1][1]

        if line.startswith("- "):
            item = line[2:].strip()
            if isinstance(container, list):
                container.append(_parse_scalar(item))
            continue

        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()

        if value == "":
            # 新子节点，默认dict；如果后续是list项，会替换为list
            new_obj: dict[str, Any] = {}
            if isinstance(container, dict):
                container[key] = new_obj
                stack.append((indent + 2, new_obj))
            continue

        parsed_value = _parse_scalar(value)
        if isinstance(container, dict):
            container[key] = parsed_value

    # 后处理：把空dict且名字以"s"结尾的字段当作空list（简单容错）
    _normalize_empty_collections(root)
    return root


def _parse_scalar(value: str) -> Any:
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.replace(".", "", 1).isdigit():
        if "." in value:
            return float(value)
        return int(value)
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def _normalize_empty_collections(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, dict) and not v and (k.endswith("s") or k.endswith("_list")):
                obj[k] = []
            else:
                _normalize_empty_collections(v)
    elif isinstance(obj, list):
        for item in obj:
            _normalize_empty_collections(item)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def extract_expected_keywords(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [value]
    return []


def render_report(template: str, values: dict[str, Any]) -> str:
    output = template
    for k, v in values.items():
        output = output.replace(f"{{{{{k}}}}}", str(v))
    return output


def run_routing_eval(
    client: httpx.Client,
    base_url: str,
    dataset: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[float]]:
    payload = {
        "samples": [
            {
                "message": row["message"],
                "expected_intent": row["expected_intent"],
            }
            for row in dataset
        ]
    }
    start = time.perf_counter()
    resp = client.post(f"{base_url}/api/eval/routing", json=payload)
    latency_ms = (time.perf_counter() - start) * 1000
    resp.raise_for_status()
    result = resp.json()
    return result, [latency_ms]


def run_rag_eval(
    client: httpx.Client,
    base_url: str,
    dataset: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[float]]:
    details: list[dict[str, Any]] = []
    latencies: list[float] = []
    hit_count = 0

    for row in dataset:
        message = row["query"]
        expected_keywords = extract_expected_keywords(row.get("expected_keywords", []))

        start = time.perf_counter()
        resp = client.post(
            f"{base_url}/api/chat",
            json={"user_id": "harness_runner", "message": message},
        )
        latency_ms = (time.perf_counter() - start) * 1000
        latencies.append(latency_ms)
        resp.raise_for_status()
        data = resp.json()
        answer = str(data.get("response", ""))

        matched = [kw for kw in expected_keywords if kw and kw in answer]
        keyword_hit = len(matched) > 0 if expected_keywords else True
        if keyword_hit:
            hit_count += 1

        details.append(
            {
                "query": message,
                "expected_keywords": expected_keywords,
                "matched_keywords": matched,
                "keyword_hit": keyword_hit,
                "intent": data.get("intent", "unknown"),
                "latency_ms": round(latency_ms, 2),
            }
        )

    return {
        "total": len(dataset),
        "keyword_hit_count": hit_count,
        "details": details,
    }, latencies


def main() -> int:
    parser = argparse.ArgumentParser(description="Run minimal harness evaluation.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()

    config = parse_simple_yaml(Path(args.config))
    routing_dataset_path = Path(config.get("datasets", {}).get("routing", str(DEFAULT_ROUTING_DATASET)))
    rag_dataset_path = Path(config.get("datasets", {}).get("rag", str(DEFAULT_RAG_DATASET)))

    thresholds_cfg = config.get("thresholds", {})
    thresholds = EvalThresholds(
        routing_accuracy_min=float(thresholds_cfg.get("routing_accuracy_min", 0.90)),
        rag_keyword_hit_min=float(thresholds_cfg.get("rag_keyword_hit_min", 0.75)),
        avg_latency_ms_max=float(thresholds_cfg.get("avg_latency_ms_max", 3000)),
    )

    routing_rows = load_jsonl(routing_dataset_path)
    rag_rows = load_jsonl(rag_dataset_path)

    with httpx.Client(timeout=30.0) as client:
        routing_result, routing_latencies = run_routing_eval(client, args.base_url, routing_rows)
        rag_result, rag_latencies = run_rag_eval(client, args.base_url, rag_rows)

    routing_accuracy = calc_accuracy(
        int(routing_result.get("correct", 0)),
        int(routing_result.get("total", 0)),
    )
    rag_keyword_hit = calc_keyword_hit_rate(
        int(rag_result.get("keyword_hit_count", 0)),
        int(rag_result.get("total", 0)),
    )
    avg_latency_ms = calc_avg_latency(routing_latencies + rag_latencies)

    threshold_result = check_thresholds(
        routing_accuracy=routing_accuracy,
        rag_keyword_hit=rag_keyword_hit,
        avg_latency_ms=avg_latency_ms,
        thresholds=thresholds,
    )

    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "timestamp_utc": now,
        "base_url": args.base_url,
        "thresholds": asdict(thresholds),
        "summary": {
            "routing_accuracy": round(routing_accuracy, 4),
            "rag_keyword_hit_rate": round(rag_keyword_hit, 4),
            "avg_latency_ms": round(avg_latency_ms, 2),
            **threshold_result,
        },
        "routing_eval": routing_result,
        "rag_eval": rag_result,
    }

    DEFAULT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    latest_json = DEFAULT_RESULTS_DIR / "latest.json"
    latest_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    template = DEFAULT_REPORT_TEMPLATE.read_text(encoding="utf-8")
    report = render_report(
        template,
        {
            "timestamp_utc": now,
            "base_url": args.base_url,
            "routing_accuracy": payload["summary"]["routing_accuracy"],
            "rag_keyword_hit_rate": payload["summary"]["rag_keyword_hit_rate"],
            "avg_latency_ms": payload["summary"]["avg_latency_ms"],
            "routing_accuracy_min": thresholds.routing_accuracy_min,
            "rag_keyword_hit_min": thresholds.rag_keyword_hit_min,
            "avg_latency_ms_max": thresholds.avg_latency_ms_max,
            "overall_passed": payload["summary"]["passed"],
            "routing_passed": payload["summary"]["routing_passed"],
            "rag_passed": payload["summary"]["rag_passed"],
            "latency_passed": payload["summary"]["latency_passed"],
            "routing_total": routing_result.get("total", 0),
            "routing_correct": routing_result.get("correct", 0),
            "rag_total": rag_result.get("total", 0),
            "rag_keyword_hit_count": rag_result.get("keyword_hit_count", 0),
        },
    )
    latest_md = DEFAULT_RESULTS_DIR / "latest.md"
    latest_md.write_text(report, encoding="utf-8")

    print(f"Harness summary: {json.dumps(payload['summary'], ensure_ascii=False)}")
    print(f"Results JSON: {latest_json}")
    print(f"Results Report: {latest_md}")

    return 0 if payload["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
