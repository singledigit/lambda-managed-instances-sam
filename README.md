# Lambda Managed Instances - Webhook Processor Demo

A working demo of AWS Lambda Managed Instances (LMI) using AWS SAM. Deploys a webhook processing API that receives events, enriches them by calling downstream services concurrently, and stores results in DynamoDB.

## What This Demonstrates

- `AWS::Serverless::CapacityProvider` resource in SAM
- Multi-concurrency for I/O-bound workloads
- Async enrichment with `asyncio` (3 concurrent downstream calls)
- Lambda Powertools for observability
- DynamoDB for event storage

## Prerequisites

- AWS CLI configured with a profile
- SAM CLI v1.140+
- Docker or Finch (for container builds)
- A VPC with at least 3 subnets across different AZs

## Deploy

```bash
sam build --use-container
sam deploy --guided
```

## Test

```bash
# Health check
curl https://<api-id>.execute-api.<region>.amazonaws.com/prod/health

# Send a webhook
curl -X POST https://<api-id>.execute-api.<region>.amazonaws.com/prod/webhooks \
  -H "Content-Type: application/json" \
  -d '{"event_type": "payment.completed", "order_id": "ORD-123", "amount": 99.99}'

# Retrieve a processed event
curl https://<api-id>.execute-api.<region>.amazonaws.com/prod/webhooks/<event_id>
```

## Cleanup

```bash
sam delete --stack-name lmi-webhook-demo --profile demo --region us-west-2
```
