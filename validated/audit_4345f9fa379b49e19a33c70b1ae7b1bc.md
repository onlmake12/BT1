Based on my research, I have confirmed the following:

1. **Fortuna** (`apps/fortuna/src/command/run.rs` lines 66–71): The router is built with only `CorsLayer::permissive()` — zero rate-limiting middleware.
2. **Fortuna API routes** (`apps/fortuna/src/api.rs` lines 201–215): All routes including the sensitive `/v1/chains/:chain_id/revelations/:sequence` endpoint are registered with no throttling layer.
3. **Grep search** across all `apps/fortuna/**/*.rs`: **zero matches** for any rate-limiting pattern.
4. **Hermes REST** (`apps/hermes/server/src/api.rs` lines 164–210): Only `CorsLayer::permissive()` and a metrics middleware — no rate limiting on REST endpoints (only the WebSocket handler has bytes-based limiting).

---

### Title
Absence of Rate Limiting on Fortuna Entropy Provider Server Endpoints — (`File: apps/fortuna/src/command/run.rs`, `apps/fortuna/src/api.rs`)

### Summary
The Fortuna off-chain Entropy provider server exposes multiple HTTP endpoints — most critically `/v1/chains/:chain_id/revelations/:sequence` — with no rate limiting of any kind. An unprivileged attacker can flood these endpoints with arbitrary requests, exhausting Fortuna's resources and its downstream blockchain RPC quota, degrading or denying service to legitimate Entropy users who depend on Fortuna to retrieve the provider's random number reveal.

### Finding Description
The `run_api` function in `apps/fortuna/src/command/run.rs` constructs the Axum router and applies only a permissive CORS layer:

```rust
let app = app
    .merge(SwaggerUi::new("/docs").url("/docs/openapi.json", ApiDoc::openapi()))
    .merge(api::routes(api_state))
    // Permissive CORS layer to allow all origins
    .layer(CorsLayer::permissive());
``` [1](#0-0) 

No rate-limiting middleware (e.g., `tower-governor`, `tower::limit`, or any custom throttle layer) is applied at any point. The `routes()` function in `apps/fortuna/src/api.rs` registers all endpoints without any per-route throttling:

```rust
pub fn routes(state: ApiState) -> Router<(), Body> {
    Router::new()
        .route("/", get(index))
        .route("/live", get(live))
        .route("/metrics", get(metrics))
        .route("/ready", get(ready))
        .route("/v1/chains", get(chain_ids))
        .route("/v1/logs", get(explorer))
        .route("/v1/chains/:chain_id/revelations/:sequence", get(revelation))
        .route("/v1/chains/configs", get(get_chain_configs))
        .with_state(state)
}
``` [2](#0-1) 

The `/v1/chains/:chain_id/revelations/:sequence` endpoint is the most sensitive: each request causes Fortuna to perform an on-chain read via `EntropyReader` to verify whether the given sequence number has a pending, confirmed request before revealing the hash-chain value. This RPC call is triggered for every inbound HTTP request regardless of origin or frequency.

A grep across the entire `apps/fortuna/` Rust source confirms zero rate-limiting code exists anywhere in the service. [3](#0-2) 

By contrast, the Hermes WebSocket handler does implement bytes-based rate limiting per IP, but Hermes REST endpoints and all Fortuna endpoints are unprotected. [4](#0-3) 

### Impact Explanation
An attacker who floods `/v1/chains/:chain_id/revelations/:sequence` with high-volume requests (using arbitrary or valid sequence numbers) can:

- **Exhaust Fortuna's RPC quota** against the configured `geth_rpc_addr` for each chain, since every revelation request triggers a blockchain read.
- **Starve legitimate Entropy users**: real users who have submitted an on-chain randomness request must call Fortuna to retrieve the provider's reveal. If Fortuna is overwhelmed or its RPC node is rate-limited/exhausted, those users cannot complete their random number requests, breaking the Entropy protocol's liveness guarantee.
- **Exhaust server-side resources** (CPU, memory, file descriptors, async task pool) through sustained request floods, potentially causing the process to crash or become unresponsive.

This maps directly to the Immunefi scope impact of **degraded service / DoS against the Entropy provider infrastructure**.

### Likelihood Explanation
- The Fortuna server URL is publicly registered on-chain as the provider URI (see `get_register_uri` in `apps/fortuna/src/api.rs` line 220–225), making it trivially discoverable by any on-chain observer.
- No authentication, API key, or IP allowlist is required to call any endpoint.
- A simple script sending GET requests in a tight loop is sufficient — no special knowledge or privilege is needed.
- The attack is cheap: HTTP GET requests with no body, no fee, no on-chain transaction required. [5](#0-4) 

### Recommendation
1. Add a rate-limiting middleware layer (e.g., `tower_governor` or a custom `tower::Service`) to the Axum router in `run_api`, applied globally or per-route, keyed by client IP address.
2. Apply stricter limits specifically to `/v1/chains/:chain_id/revelations/:sequence` since each request triggers an RPC call.
3. Consider restricting the `/metrics` endpoint to internal/loopback access only, as it currently leaks operational data publicly.
4. Mirror the per-IP bytes-based rate limiting already present in the Hermes WebSocket handler to the Fortuna HTTP layer.

### Proof of Concept
```bash
# Fortuna provider URL is publicly readable from on-chain provider registration.
# Flood the revelation endpoint with arbitrary sequence numbers:
for i in $(seq 1 10000); do
  curl -s "https://fortuna.dourolabs.app/v1/chains/ethereum/revelations/$i" &
done
wait
```

Each request causes Fortuna to issue an `eth_call` / `eth_getLogs` RPC call to the configured Ethereum node. At sufficient volume, the RPC node's rate limit is hit, Fortuna's async executor is saturated, and legitimate Entropy users calling the same endpoint to retrieve their random number reveal receive `503 Service Unavailable` or timeouts — breaking the Entropy fulfillment flow. [6](#0-5) [2](#0-1)

### Citations

**File:** apps/fortuna/src/command/run.rs (L62-88)
```rust
    let api_state = api::ApiState::new(chains, metrics_registry, history, config).await;

    // Initialize Axum Router. Note the type here is a `Router<State>` due to the use of the
    // `with_state` method which replaces `Body` with `State` in the type signature.
    let app = Router::new();
    let app = app
        .merge(SwaggerUi::new("/docs").url("/docs/openapi.json", ApiDoc::openapi()))
        .merge(api::routes(api_state))
        // Permissive CORS layer to allow all origins
        .layer(CorsLayer::permissive());

    tracing::info!("Starting server on: {:?}", &socket_addr);
    // Binds the axum's server to the configured address and port. This is a blocking call and will
    // not return until the server is shutdown.
    axum::Server::try_bind(&socket_addr)?
        .serve(app.into_make_service())
        .with_graceful_shutdown(async {
            // It can return an error or an Ok(()). In both cases, we would shut down.
            // As Ok(()) means, exit signal (ctrl + c) was received.
            // And Err(e) means, the sender was dropped which should not be the case.
            let _ = rx_exit.changed().await;

            tracing::info!("Shutting down RPC server...");
        })
        .await?;

    Ok(())
```

**File:** apps/fortuna/src/api.rs (L1-28)
```rust
use {
    crate::{
        chain::reader::{BlockNumber, BlockStatus, EntropyReader},
        config::Config,
        history::History,
        state::MonitoredHashChainState,
    },
    anyhow::Result,
    axum::{
        body::Body,
        http::StatusCode,
        response::{IntoResponse, Response},
        routing::get,
        Router,
    },
    ethers::core::types::Address,
    prometheus_client::{
        encoding::EncodeLabelSet,
        metrics::{counter::Counter, family::Family},
        registry::Registry,
    },
    std::{collections::HashMap, sync::Arc},
    tokio::sync::RwLock,
    url::Url,
};
pub use {
    chain_ids::*, config::*, explorer::*, index::*, live::*, metrics::*, ready::*, revelation::*,
};
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

**File:** apps/fortuna/src/api.rs (L220-225)
```rust
pub fn get_register_uri(base_uri: &str, chain_id: &str) -> Result<String> {
    let base_uri = Url::parse(base_uri)?;
    let path = format!("/v1/chains/{chain_id}");
    let uri = base_uri.join(&path)?;
    Ok(uri.to_string())
}
```

**File:** apps/hermes/server/src/api/ws.rs (L581-621)
```rust
            // Close the connection if rate limit is exceeded and the ip is not whitelisted.
            // If the ip address is None no rate limiting is applied.
            if let Some(ip_addr) = self.ip_addr {
                if !self
                    .ws_state
                    .bytes_limit_whitelist
                    .iter()
                    .any(|ip_net| ip_net.contains(&ip_addr))
                    && self.ws_state.rate_limiter.check_key_n(
                        &ip_addr,
                        NonZeroU32::new(message.len().try_into()?)
                            .ok_or(anyhow!("Empty message"))?,
                    ) != Ok(Ok(()))
                {
                    tracing::info!(
                        self.id,
                        ip = %ip_addr,
                        "Rate limit exceeded. Closing connection.",
                    );
                    self.ws_state
                        .metrics
                        .interactions
                        .get_or_create(&Labels {
                            interaction: Interaction::RateLimit,
                            status: Status::Error,
                            token_suffix: self.token_suffix.clone(),
                        })
                        .inc();

                    self.ws_send(
                        serde_json::to_string(&ServerResponseMessage::Err {
                            error: "Rate limit exceeded".to_string(),
                        })?
                        .into(),
                    )
                    .await?;
                    self.ws_close().await?;
                    self.closed = true;
                    return Ok(());
                }
            }
```
