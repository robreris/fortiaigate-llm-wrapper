# FortiAIGate Private Endpoint LLM Wrapper

A lightweight proxy service that bridges FortiAIGate to the OpenAI Responses API with autonomous MCP tool use.

---

## Architecture

```
User (Browser)
    │
    ▼
FortiGate (perimeter NGFW)
    │
    ▼
Chatbot Application
    │
    ▼
FortiAIGate  ──── AI Guard scans (prompt injection, DLP, toxicity)
    │
    ▼
LLM Wrapper  ◄─── this service
    │
    ▼
OpenAI Responses API
    │
    └──► autonomous MCP tool calls
              │
              ▼
         FortiWeb (public MCP gateway)
              │
              ▼
         MCP Server (private)
```

### What the wrapper does

FortiAIGate is configured to send requests to an upstream LLM using the OpenAI Chat Completions API format — the same way it connects to providers like Cohere. This service is that upstream endpoint.

On each request it:

1. **Receives** an OpenAI-compatible `POST /v1/chat/completions` request from FortiAIGate, containing the user prompt (already scanned and cleared by FortiAIGate's AI Guard).
2. **Transforms** the request for the OpenAI Responses API: splits `system` messages into the `instructions` parameter and passes the remaining conversation as `input`.
3. **Injects MCP configuration** — adds FortiWeb as an MCP tool server so OpenAI can make autonomous outbound tool calls to FortiWeb during inference. FortiWeb acts as a public gateway to the private MCP server behind it.
4. **Forwards** to OpenAI and streams or buffers the response.
5. **Returns** an OpenAI Chat Completions-shaped response to FortiAIGate so it can relay the answer back to the chatbot.

The OpenAI Responses API (used here rather than Chat Completions) has native support for the `mcp` tool type, which lets OpenAI autonomously discover and call MCP tools without the wrapper needing to manage the tool-call loop itself.

### Why EKS (same cluster as FortiAIGate)

The wrapper runs as a `ClusterIP` service in the `fortiaigate` namespace alongside FortiAIGate. This means:

- FortiAIGate reaches it via in-cluster DNS (`http://llm-wrapper.fortiaigate.svc.cluster.local:8080/v1`) — no public endpoint needed.
- No additional AWS infrastructure cost or cross-service latency.
- Security posture matches the existing FortiAIGate deployment (same uid, capabilities, seccomp profile).

---

## Prerequisites

- Access to the EKS cluster deployed by `fortiaigate-terraform-helm` (`kubectl` configured and pointing at it)
- An ECR repository to host the wrapper image
- An OpenAI API key with access to `gpt-4o` (or your chosen model)
- The publicly reachable FortiWeb MCP transport endpoint URL

Verify your kubectl context before proceeding:

```bash
kubectl config current-context
kubectl get nodes -n fortiaigate
```

---

## Deployment

### 1. Build and push the container image

Create an ECR repository if one does not exist:

```bash
aws ecr create-repository \
  --repository-name llm-wrapper \
  --region <YOUR_AWS_REGION>
```

Authenticate Docker to ECR, then build and push:

```bash
export ECR_URI=<YOUR_ACCOUNT_ID>.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com/llm-wrapper

aws ecr get-login-password --region <YOUR_AWS_REGION> \
  | docker login --username AWS --password-stdin $(echo $ECR_URI | cut -d/ -f1)

docker build -t $ECR_URI:latest .
docker push $ECR_URI:latest
```

### 2. Ensure the EKS node IAM role can pull from ECR

The EKS managed node group IAM role needs the `AmazonEC2ContainerRegistryReadOnly` policy attached. Check whether it already is:

```bash
aws iam list-attached-role-policies \
  --role-name <NODE_GROUP_IAM_ROLE_NAME> \
  --query 'AttachedPolicies[].PolicyName'
```

If `AmazonEC2ContainerRegistryReadOnly` is absent, attach it:

```bash
aws iam attach-role-policy \
  --role-name <NODE_GROUP_IAM_ROLE_NAME> \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
```

The node group role name is visible in the AWS console under EKS → your cluster → Compute → Node groups, or in the Terraform outputs.

### 3. Update the image reference

Edit `k8s/deployment.yaml` and replace the placeholder image with your ECR URI:

```yaml
image: <YOUR_ACCOUNT_ID>.dkr.ecr.<YOUR_AWS_REGION>.amazonaws.com/llm-wrapper:latest
```

### 4. Create the Kubernetes secret

There are two options for this step. 

A) Update .env and then source it and pipe through envsubst before applying:

```
cp .env.example .env
set -a && source .env && set +a
envsubst < k8s/secret.yaml | kubectl apply -f -
```

B) Use kubectl inline commands. 

```bash
kubectl create secret generic llm-wrapper-secrets \
  --namespace fortiaigate \
  --from-literal=OPENAI_API_KEY="sk-..." \
  --from-literal=MCP_SERVER_URL="https://fortiweb.example.com/mcp" \
  --from-literal=MCP_SERVER_LABEL="fortiweb" \
  --from-literal=DEFAULT_MODEL="gpt-4o" \
  --from-literal=MCP_REQUIRE_APPROVAL="never"
  # --from-literal=MCP_API_KEY="your-token"  # add if the MCP server requires authentication
```

`MCP_SERVER_URL` is the full publicly reachable FortiWeb MCP transport endpoint that OpenAI will call. Use the route FortiWeb exposes for the selected transport, for example `/mcp`, `/sse`, or another configured path.

`MCP_SERVER_LABEL` is the identifier string attached to the MCP tool in OpenAI requests. Set it to something meaningful for the server you are pointing at (e.g. `fortiweb`).

`MCP_REQUIRE_APPROVAL` controls whether OpenAI must confirm each MCP tool call. `never` is appropriate for fully autonomous agentic use; set to `always` if you want human-in-the-loop approval.

### 5. Apply the Kubernetes manifests

```bash
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
```

Verify the pod comes up:

```bash
kubectl get pods -n fortiaigate -l app=llm-wrapper
kubectl logs -n fortiaigate -l app=llm-wrapper
```

The readiness probe hits `/health` every 10 seconds. Once the pod shows `Running` and `READY 1/1`, it is accepting traffic.

### 6. Restrict network access with a NetworkPolicy

By default any pod in the cluster can reach the wrapper. Limit ingress to FortiAIGate's `core` pod only:

```bash
kubectl apply -f k8s/networkpolicy.yaml
```

This policy depends on FortiAIGate core pods using the `app: core` label and the wrapper pod using `app: llm-wrapper`, which match the default manifests.

### 7. Configure FortiAIGate to use the wrapper

In the FortiAIGate admin UI, add a new upstream LLM provider with these settings:

| Field | Value |
|-------|-------|
| Provider type | OpenAI (or OpenAI-compatible) |
| API Base URL | `http://llm-wrapper.<NAMESPACE>.svc.cluster.local:8080/v1` |
| API Key | any non-empty string (ignored by the wrapper) |
| Model | `gpt-4o` (must match `DEFAULT_MODEL` in the secret) |

Replace `<NAMESPACE>` with the Kubernetes namespace FortiAIGate is deployed into (the `namespace` variable in `fortiaigate-terraform-helm`, defaulting to `fortiaigate`). The `cluster.local` portion is the Kubernetes cluster DNS domain — it is **not** the EKS cluster name and does not change between clusters unless the DNS domain was explicitly customized at cluster creation (uncommon).

With the default namespace the URL is:
```
http://llm-wrapper.fortiaigate.svc.cluster.local:8080/v1
```

The wrapper is reached over plain HTTP on the in-cluster network. TLS is not required for this link — both pods run in the same namespace and AWS encrypts node-to-node VPC traffic at the network layer.

> **Note:** Do not reduce `UPSTREAM_LLM_REQUEST_TIMEOUT` below its default of 600 seconds in the FortiAIGate Helm values. Agentic requests involving multiple MCP tool call round-trips can legitimately take several minutes.

---

## Smoke testing

### Testing without FortiWeb (public MCP server)

If you don't yet have a FortiWeb MCP endpoint, you can substitute any publicly hosted, unauthenticated remote MCP server for local testing. Two good options:

| Option | `MCP_SERVER_URL` | `MCP_SERVER_LABEL` | Notes |
|--------|-----------------|-------------------|-------|
| **GitMCP** | `https://gitmcp.io/{owner}/{repo}` | `gitmcp` | Turns any public GitHub repo into an MCP server. Replace `{owner}/{repo}` with any public repo (e.g. `https://gitmcp.io/openai/tiktoken`). No auth required. |
| **Context7** | `https://mcp.context7.com/mcp` | `context7` | Library/framework documentation lookup. No auth required for basic use. Default `MCP_SERVER_LABEL` in config. |

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```dotenv
OPENAI_API_KEY=sk-...
MCP_SERVER_URL=https://gitmcp.io/openai/tiktoken
MCP_SERVER_LABEL=gitmcp
# MCP_API_KEY is not needed for these servers — leave it unset
```

Then run locally:

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8080
```

### From outside the cluster (local uvicorn)

Run the app locally to verify OpenAI connectivity before deploying:

```bash
pip install -r requirements.txt
export OPENAI_API_KEY="sk-..."
export MCP_SERVER_URL="https://fortiweb.example.com/mcp"
uvicorn app.main:app --port 8080
```

```bash
# Health check
curl -s http://localhost:8080/health

# Models list
curl -s http://localhost:8080/v1/models | jq .

# Non-streaming completion
curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"ping"}],"stream":false}' | jq .

# Streaming completion
curl -N -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"ping"}],"stream":true}'
```

### From inside the cluster

Spin up a temporary curl pod in the same namespace:

```bash
kubectl run curl-test -n fortiaigate --image=curlimages/curl --rm -it --restart=Never -- \
  curl -s http://llm-wrapper:8080/health
```

Full round-trip test reachable by FortiAIGate:

```bash
kubectl run curl-test -n fortiaigate --image=curlimages/curl --rm -it --restart=Never -- \
  curl -s -X POST http://llm-wrapper:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"What tools do you have?"}],"stream":false}'
```

---

## Configuration reference

All configuration is supplied via environment variables (injected from the `llm-wrapper-secrets` Kubernetes secret).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENAI_API_KEY` | Yes | — | OpenAI API key |
| `MCP_SERVER_URL` | Yes | — | Full MCP transport endpoint URL (e.g. `https://fortiweb.example.com/mcp`) |
| `MCP_SERVER_LABEL` | No | `context7` | Identifier string attached to the MCP tool in OpenAI requests; appears in tool-call events in the response stream |
| `MCP_API_KEY` | No | — | Bearer token forwarded to the MCP server in an `Authorization` header. Omit if the server requires no authentication |
| `DEFAULT_MODEL` | No | `gpt-4o` | OpenAI model to use when the request does not specify one |
| `MCP_REQUIRE_APPROVAL` | No | `never` | `never` for autonomous tool use; `always` for human-in-the-loop |
| `LOG_LEVEL` | No | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## Updating the deployment

To deploy a new image version:

```bash
docker build -t $ECR_URI:latest .
docker push $ECR_URI:latest
kubectl rollout restart deployment/llm-wrapper -n fortiaigate
kubectl rollout status deployment/llm-wrapper -n fortiaigate
```

To update a secret value (e.g. rotate the OpenAI API key):

```bash
kubectl patch secret llm-wrapper-secrets -n fortiaigate \
  --type='json' \
  -p='[{"op":"replace","path":"/data/OPENAI_API_KEY","value":"'$(echo -n "sk-newkey..." | base64)'"}]'
kubectl rollout restart deployment/llm-wrapper -n fortiaigate
```
