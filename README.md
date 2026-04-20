# Lambda Managed Instances — Webhook Processor Demo

A working demo of [AWS Lambda Managed Instances](https://docs.aws.amazon.com/lambda/latest/dg/lambda-managed-instances.html) (LMI) using AWS SAM. Deploys a webhook processing API that receives events, enriches them by calling downstream services concurrently, and stores results in DynamoDB.

Read the companion blog post: [Lambda Managed Instances: A Working Demo and the Math Behind It](https://edjgeek.com/blog/lmi-webhook-processor)

## What This Demonstrates

- `AWS::Serverless::CapacityProvider` — the new SAM resource for LMI
- `CapacityProviderConfig` on `AWS::Serverless::Function` — attaching a function to a capacity provider
- Multi-concurrency for I/O-bound workloads (16 concurrent invocations per vCPU)
- Async enrichment with `asyncio.gather` (3 concurrent downstream calls)
- Lambda Powertools for structured logging, tracing, and metrics
- `AutoPublishAlias` for automatic version publishing (required for LMI)

## Architecture

API Gateway → Lambda (LMI) → 3 concurrent enrichment calls + DynamoDB write

The Lambda function simulates a webhook processor that validates incoming events, calls three downstream services concurrently (geocoding, fraud scoring, loyalty lookup), and writes the enriched result to DynamoDB. Total processing time is ~200ms, with the CPU active for only ~10% of that — the rest is I/O wait. This is the pattern where LMI's multi-concurrency saves money.

## Prerequisites

- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) configured with credentials
- [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) v1.140+
- [Docker](https://docs.docker.com/get-docker/) or [Finch](https://github.com/runfinch/finch) (required for `--use-container` builds)
- A VPC with at least 3 subnets across different Availability Zones and outbound internet access (the default VPC works)
- LMI is available in: us-east-1, us-east-2, us-west-2, ap-northeast-1, eu-west-1

## Deploy

### 1. Build

```bash
sam build --use-container
```

The `--use-container` flag is needed because the build requires Python 3.13, which may not be installed locally.

### 2. Deploy

```bash
sam deploy --guided
```

SAM will prompt you for:

| Parameter | Description | Example |
|-----------|-------------|---------|
| Stack Name | CloudFormation stack name | `lmi-webhook-demo` |
| AWS Region | Must be an LMI-supported region | `us-west-2` |
| Subnet1 | First subnet ID (AZ 1) | `subnet-abc123` |
| Subnet2 | Second subnet ID (AZ 2) | `subnet-def456` |
| Subnet3 | Third subnet ID (AZ 3) | `subnet-ghi789` |
| SecurityGroupId | Security group allowing outbound traffic | `sg-xyz000` |

To find your default VPC subnets and security group:

```bash
# Get default VPC ID
aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" --query "Vpcs[0].VpcId" --output text

# List subnets (pick 3 from different AZs)
aws ec2 describe-subnets --filters "Name=vpc-id,Values=<vpc-id>" --query "Subnets[*].[SubnetId,AvailabilityZone]" --output table

# Get default security group
aws ec2 describe-security-groups --filters "Name=vpc-id,Values=<vpc-id>" "Name=group-name,Values=default" --query "SecurityGroups[0].GroupId" --output text
```

> **Note:** Deployment takes 3-5 minutes. LMI provisions EC2 instances and initializes execution environments before the function becomes invocable.

### 3. Get the API endpoint

After deployment, SAM prints the outputs. Grab the `ApiEndpoint` value:

```bash
aws cloudformation describe-stacks --stack-name <stack-name> --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text
```

## Test

Replace `<api-endpoint>` with your API Gateway URL from the deploy output.

### Health check

```bash
curl <api-endpoint>/health
```

```json
{
  "status": "healthy",
  "compute_type": "lambda-managed-instances"
}
```

### Send a webhook

```bash
curl -X POST <api-endpoint>/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "payment.completed",
    "order_id": "ORD-12345",
    "amount": 99.99,
    "customer_id": "CUST-789"
  }'
```

```json
{
  "event_id": "7d660175-...",
  "event_type": "payment.completed",
  "status": "processed",
  "processing_time_ms": 202.0,
  "enrichment": {
    "geocoding": { "latency_ms": 151.0, "status": "success" },
    "fraud-scoring": { "latency_ms": 201.3, "status": "success" },
    "loyalty-lookup": { "latency_ms": 100.6, "status": "success" }
  }
}
```

### Retrieve a processed event

```bash
curl <api-endpoint>/webhooks/<event_id>
```

## Cleanup

> **Important:** LMI EC2 instances run 24/7 and bill continuously. Delete the stack when you're done.

```bash
sam delete --stack-name <stack-name>
```

This tears down everything: the capacity provider, EC2 instances, Lambda function, API Gateway, and DynamoDB table.

## Cost

While deployed, this demo runs 3 EC2 instances (c7a.2xlarge or similar, chosen by Lambda). Expect roughly $2-3/day in EC2 costs plus the 15% LMI management fee. Delete the stack when you're not actively testing.

## Project Structure

```
├── template.yaml                              # SAM template (capacity provider + function + API)
└── infrastructure/
    └── lambda/
        └── webhook-processor/
            ├── app.py                         # Lambda handler with async enrichment
            └── requirements.txt               # Dependencies (none beyond runtime + layer)
```

## License

MIT
