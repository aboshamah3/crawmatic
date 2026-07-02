# Contract: API Health Endpoint

The **only** application behaviour in scope for SPEC-01. The API service exposes a single, unauthenticated liveness endpoint and **no other business logic** (FR-005, spec US1 AS2, SC-002).

## `GET /health`

- **Method / path**: `GET /health`
- **Auth**: none.
- **Request body**: none.
- **Query/path params**: none.
- **Dependencies**: MUST NOT require a live database, Redis, or Scrapyd connection. It reflects only that the API process is up and serving. (A readiness check that touches PgBouncer may be added in a later spec; liveness here is dependency-free so it returns 200 even before Postgres accepts connections — see spec Edge Cases.)

### Response — 200 OK

```json
{
  "status": "ok"
}
```

- `Content-Type: application/json`
- Returns 200 whenever the process is serving (SC-002: succeeds 100% of the time once the API is up).

### Behavioural requirements

| Requirement | Source |
|-------------|--------|
| Endpoint exists and returns a success response indicating the service is up. | FR-005, US1 AS2 |
| No business logic beyond health checks at this stage. | FR-005 |
| If a per-request DB engine were created here it would leak pooled connections — the handler MUST NOT construct an engine; any future readiness variant reuses the process-wide lazy engine. | §4 engine hygiene, FR-020 |

### OpenAPI sketch (informative)

```yaml
paths:
  /health:
    get:
      summary: Liveness probe
      operationId: getHealth
      responses:
        "200":
          description: Service is up
          content:
            application/json:
              schema:
                type: object
                required: [status]
                properties:
                  status:
                    type: string
                    enum: [ok]
```

### Validation (see quickstart.md)

- `curl -fsS http://localhost:${API_PORT}/health` returns HTTP 200 and `{"status":"ok"}`.
- Compose healthcheck for the `api` service uses this endpoint to gate "healthy".
