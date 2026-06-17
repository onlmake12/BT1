### Title
Lack of Rate Limiting on Fortuna Entropy Provider API Enables Resource Exhaustion DoS - (File: apps/fortuna/src/command/run.rs)

### Summary
The Fortuna Entropy provider HTTP API is publicly accessible at `https://fortuna.pyth.network` and exposes multiple endpoints with no rate limiting. The most critical is `GET /v1/chains/{chain_id}/revelations/{sequence}`, which triggers two expensive blockchain RPC calls per request. An unprivileged attacker can flood this endpoint with parallel requests, exhausting RPC node connections and Fortuna server resources, preventing legitimate Entropy users from retrieving provider revelations needed to complete the randomness protocol.

### Finding Description
The `run_api` function in `apps/fortuna/src/command/run.rs` builds the Axum router with only a permissive CORS layer — no rate limiting middleware of any kind is applied:

```rust
let app = app
    .merge(SwaggerUi::new("/docs").url("/docs/openapi.json", ApiDoc::openapi()))
    .merge(api::routes(api_state))
    // Permissive CORS layer to allow all origins
    .layer(CorsLayer::permissive());
``` [1](#0-0) 

The `revelation` handler in `apps/fortmes/src/api/revelation.rs` issues two concurrent RPC calls to the blockchain node on every request — `get_block_number` and `get_request_v2` — via `try_join!`:

```rust
let (maybe_request, current_block_number) =
    try_join!(maybe_request_fut, current_block_number_fut)...
``` [2](#0-1) 

The `explorer` endpoint (`GET /v1/logs`) similarly issues two parallel database queries per request with no rate limiting: [3](#0-2) 

Neither the router definition nor any middleware layer applies per-IP or global request throttling: [4](#0-3) 

The Fortuna `Cargo.toml` includes `tower-http` only for CORS — no `tower` governor, rate-limit, or concurrency-limit features are present: [5](#0-4) 

### Impact Explanation
The Fortuna API is a required component of the Pyth Entropy protocol. After a user submits a randomness request on-chain, they must call `GET /v1/chains/{chain_id}/revelations/{sequence}` to retrieve the provider's committed random value and complete the reveal transaction. If this endpoint is made unavailable or unresponsive:

- Entropy users cannot complete the two-party randomness protocol, leaving their on-chain requests permanently pending.
- The Fortuna keeper itself polls the same RPC nodes; flooding the API exhausts shared RPC rate limits, degrading the keeper's ability to auto-fulfill `requestWithCallback` requests.
- The `explorer` endpoint (`/v1/logs`) triggers two parallel SQLite/Postgres queries per request; a flood of requests with `limit=1000` and wide timestamp ranges can saturate the database connection pool.

**Impact: High** — complete denial of the Entropy provider service for all users on all supported chains.

### Likelihood Explanation
The endpoint is publicly documented at `https://fortuna.pyth.network/docs` and requires no authentication. Any unprivileged actor can send thousands of parallel GET requests with arbitrary `chain_id` and `sequence` values. The attack requires no on-chain interaction, no tokens, and no special knowledge beyond the public OpenAPI spec. The `block_number` query parameter path also triggers `get_request_with_callback_events`, an even heavier RPC call. [6](#0-5) 

**Likelihood: High** — trivially exploitable by any external actor with HTTP access.

### Recommendation
1. Add a per-IP rate limiting layer to the Axum router using `tower_governor` or a similar middleware, applied before the route handlers.
2. Apply a global concurrency limit (`tower::limit::ConcurrencyLimitLayer`) to cap simultaneous in-flight RPC calls.
3. For the `explorer` endpoint, enforce the documented `limit ≤ 1000` cap in the query builder and add a minimum `min_timestamp`/`max_timestamp` window to prevent unbounded scans.
4. Return `429 Too Many Requests` with a `Retry-After` header when limits are exceeded.

### Proof of Concept
```bash
# Flood the revelation endpoint with 500 parallel requests
# No authentication required; chain_id and sequence are public knowledge
seq 1 500 | xargs -P 500 -I{} curl -s \
  "https://fortuna.pyth.network/v1/chains/ethereum/revelations/{}" &

# Simultaneously flood the explorer endpoint with expensive DB queries
seq 1 200 | xargs -P 200 -I{} curl -s \
  "https://fortuna.pyth.network/v1/logs?limit=1000&min_timestamp=2023-01-01T00:00:00Z&max_timestamp=2033-01-01T00:00:00Z" &
```

Each revelation request triggers two RPC calls to the Ethereum node. At 500 concurrent requests, this exhausts the RPC provider's connection pool and rate limits, causing the Fortuna keeper's own blockchain monitoring to fail and leaving all pending Entropy requests unfulfilled. [7](#0-6)

### Citations

**File:** apps/fortuna/src/command/run.rs (L66-71)
```rust
    let app = Router::new();
    let app = app
        .merge(SwaggerUi::new("/docs").url("/docs/openapi.json", ApiDoc::openapi()))
        .merge(api::routes(api_state))
        // Permissive CORS layer to allow all origins
        .layer(CorsLayer::permissive());
```

**File:** apps/fortuna/src/api/revelation.rs (L33-47)
```rust
pub async fn revelation(
    State(state): State<crate::api::ApiState>,
    Path(RevelationPathParams { chain_id, sequence }): Path<RevelationPathParams>,
    Query(RevelationQueryParams {
        encoding,
        block_number,
    }): Query<RevelationQueryParams>,
) -> Result<Json<GetRandomValueResponse>, RestError> {
    state
        .metrics
        .http_requests
        .get_or_create(&RequestLabel {
            value: "/v1/chains/{chain_id}/revelations/{sequence}".to_string(),
        })
        .inc();
```

**File:** apps/fortuna/src/api/revelation.rs (L68-80)
```rust
    match block_number {
        Some(block_number) => {
            let maybe_request_fut = state.contract.get_request_with_callback_events(
                block_number,
                block_number,
                state.provider_address,
            );

            let (maybe_request, current_block_number) =
                try_join!(maybe_request_fut, current_block_number_fut).map_err(|e| {
                    tracing::error!(chain_id = chain_id, "RPC request failed {}", e);
                    RestError::TemporarilyUnavailable
                })?;
```

**File:** apps/fortuna/src/api/revelation.rs (L95-99)
```rust
            let (maybe_request, current_block_number) =
                try_join!(maybe_request_fut, current_block_number_fut).map_err(|e| {
                    tracing::error!(chain_id = chain_id, "RPC request failed {}", e);
                    RestError::TemporarilyUnavailable
                })?;
```

**File:** apps/fortuna/src/api/explorer.rs (L174-177)
```rust
    let (requests, total_results) = tokio::join!(
        measure_latency(results_latency, query_tags, query.execute()),
        measure_latency(count_latency, query_tags, query.count_results())
    );
```

**File:** apps/fortuna/src/api.rs (L201-215)
```rust
pub fn routes(state: ApiState) -> Router<(), Body> {
    Router::new()
        .route("/", get(index))
        .route("/live", get(live))
        .route("/metrics", get(metrics))
        .route("/ready", get(ready))
        .route("/v1/chains", get(chain_ids))
        .route("/v1/logs", get(explorer))
        .route(
            "/v1/chains/:chain_id/revelations/:sequence",
            get(revelation),
        )
        .route("/v1/chains/configs", get(get_chain_configs))
        .with_state(state)
}
```

**File:** apps/fortuna/Cargo.toml (L33-33)
```text
tower-http = { version = "0.4.0", features = ["cors"] }
```
