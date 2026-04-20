"""Webhook processor running on Lambda Managed Instances.

Receives webhook events, validates them, simulates downstream API enrichment
(geocoding, fraud scoring, loyalty lookup), and writes enriched results to DynamoDB.

The enrichment step uses asyncio.sleep to simulate real network I/O latency.
In production, these would be actual HTTP calls to downstream services.
The point: while one invocation sleeps waiting on I/O, LMI lets other
concurrent invocations use the same CPU. Standard Lambda can't do that.
"""

import json
import os
import time
import uuid
import hashlib
import hmac
import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
tracer = Tracer()
metrics = Metrics()

app = APIGatewayRestResolver()

# Initialize outside handler for reuse across concurrent invocations.
# These are thread-safe: boto3 resources and Powertools instances
# are designed for this pattern.
TABLE_NAME = os.environ.get("TABLE_NAME", "webhook-events")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "demo-secret-key")


# --- Simulated downstream services ---

async def _simulate_api_call(service_name: str, delay_s: float) -> dict:
    """Simulate a downstream API call with realistic I/O latency.

    In production, this would be an HTTP call to a geocoding API,
    fraud scoring service, or loyalty system. We use asyncio.sleep
    to create the same I/O wait pattern without external dependencies.
    """
    start = time.monotonic()
    await asyncio.sleep(delay_s)
    elapsed_ms = (time.monotonic() - start) * 1000

    # Return mock enrichment data
    mock_responses = {
        "geocoding": {"country": "US", "region": "us-west-2", "city": "Portland"},
        "fraud-scoring": {"score": 0.12, "risk_level": "low", "checks_passed": 5},
        "loyalty-lookup": {"tier": "gold", "points": 4250, "member_since": "2021-03"},
    }

    return {
        "service": service_name,
        "status": "success",
        "latency_ms": round(elapsed_ms, 1),
        "data": mock_responses.get(service_name, {}),
    }


async def enrich_event(event_data: dict) -> dict:
    """Call 3 downstream services concurrently to enrich the webhook event.

    This is where multi-concurrency shines. Each call waits 100-200ms
    on I/O. With standard Lambda, you pay for that wait time per invocation.
    With LMI, while this invocation waits, other concurrent invocations
    share the CPU — you stop paying for idle time.
    """
    tasks = [
        _simulate_api_call("geocoding", 0.15),
        _simulate_api_call("fraud-scoring", 0.20),
        _simulate_api_call("loyalty-lookup", 0.10),
    ]
    results = await asyncio.gather(*tasks)

    enrichment = {}
    for result in results:
        enrichment[result["service"]] = result

    return enrichment


def _run_enrichment(event_data: dict) -> dict:
    """Bridge sync handler to async enrichment."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(enrich_event(event_data))
    finally:
        loop.close()


# --- Validation ---

def validate_signature(payload: str, signature: str) -> bool:
    """Validate webhook HMAC-SHA256 signature."""
    if not signature:
        return False
    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


# --- API Routes ---

@app.post("/webhooks")
@tracer.capture_method
def process_webhook():
    """Receive, validate, enrich, and store a webhook event."""
    start_time = time.monotonic()

    body = app.current_event.body or ""
    signature = app.current_event.get_header_value(
        "x-webhook-signature", default_value=""
    )

    # Parse payload
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        metrics.add_metric(name="ValidationErrors", unit=MetricUnit.Count, value=1)
        return {"statusCode": 400, "body": {"error": "Invalid JSON payload"}}

    # Validate signature (skip if none provided — demo mode)
    if signature and not validate_signature(body, signature):
        metrics.add_metric(name="ValidationErrors", unit=MetricUnit.Count, value=1)
        return {"statusCode": 401, "body": {"error": "Invalid signature"}}

    event_id = str(uuid.uuid4())
    event_type = payload.get("event_type", "unknown")
    timestamp = datetime.now(timezone.utc).isoformat()

    logger.info("Processing webhook", extra={
        "event_id": event_id,
        "event_type": event_type,
    })

    # Enrich by calling downstream services concurrently
    enrichment = _run_enrichment(payload)

    total_enrichment_ms = sum(
        r.get("latency_ms", 0) for r in enrichment.values()
    )

    processing_time = round((time.monotonic() - start_time) * 1000, 1)

    # Write to DynamoDB
    item = {
        "PK": f"EVENT#{event_type}",
        "SK": f"{timestamp}#{event_id}",
        "event_id": event_id,
        "event_type": event_type,
        "payload": json.dumps(payload),
        "enrichment": json.dumps(enrichment),
        "created_at": timestamp,
        "processing_time_ms": Decimal(str(processing_time)),
    }
    table.put_item(Item=item)

    metrics.add_metric(name="WebhooksProcessed", unit=MetricUnit.Count, value=1)
    metrics.add_metric(
        name="ProcessingTime", unit=MetricUnit.Milliseconds, value=processing_time
    )
    metrics.add_metric(
        name="EnrichmentTime", unit=MetricUnit.Milliseconds, value=total_enrichment_ms
    )

    logger.info("Webhook processed", extra={
        "event_id": event_id,
        "processing_time_ms": processing_time,
        "enrichment_time_ms": total_enrichment_ms,
    })

    return {
        "event_id": event_id,
        "event_type": event_type,
        "status": "processed",
        "processing_time_ms": processing_time,
        "enrichment": enrichment,
    }


@app.get("/webhooks/<event_id>")
@tracer.capture_method
def get_webhook(event_id: str):
    """Look up a processed webhook event by scanning for the event_id."""
    # In production you'd use a GSI on event_id. For this demo, scan is fine.
    response = table.scan(
        FilterExpression="event_id = :eid",
        ExpressionAttributeValues={":eid": event_id},
        Limit=100,
    )

    items = response.get("Items", [])
    if not items:
        return {"statusCode": 404, "body": {"error": "Event not found"}}

    item = items[0]
    return {
        "event_id": item["event_id"],
        "event_type": item["event_type"],
        "payload": json.loads(item["payload"]),
        "enrichment": json.loads(item["enrichment"]),
        "created_at": item["created_at"],
        "processing_time_ms": float(item.get("processing_time_ms", 0)),
    }


@app.get("/health")
def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "webhook-processor",
        "compute_type": "lambda-managed-instances",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@logger.inject_lambda_context(
    correlation_id_path=correlation_paths.API_GATEWAY_REST
)
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict, context: LambdaContext) -> dict:
    return app.resolve(event, context)
