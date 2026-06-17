### Title
Lazer Pusher and Hermes Client Accept Unencrypted WebSocket Endpoint Schemes Without Warning or Rejection - (File: `apps/pyth-lazer-pusher/pusher-base/src/lazer/config.rs`)

---

### Summary

The `LazerConfig` struct used by the Pyth Lazer Pusher accepts arbitrary WebSocket endpoint URLs (including unencrypted `ws://`) without scheme validation or any warning. The `access_token` credential is then transmitted over whatever connection is established. An attacker on the same network as a misconfigured pusher can perform a man-in-the-middle attack to steal the access token and inject tampered price data into the pusher pipeline. The same pattern exists in the deprecated `PriceServiceConnection` JS client, where `makeWebsocketUrl` silently downgrades `http://` to `ws://` and appends the `ACCESS_TOKEN` as a plaintext query parameter.

---

### Finding Description

`LazerConfig` in `apps/pyth-lazer-pusher/pusher-base/src/lazer/config.rs` deserializes the `endpoints` field as `Vec<Url>` with no scheme constraint:

```rust
pub struct LazerConfig {
    pub endpoints: Vec<Url>,
    #[derivative(Debug = "ignore")]
    pub access_token: String,
    ...
}
``` [1](#0-0) 

The `validate_config` function in `apps/pyth-lazer-pusher/bulk-trade-pusher/src/config.rs` only checks that `bulk.endpoints` is non-empty and that the signing key file exists — it performs **no scheme validation** on the Lazer endpoints:

```rust
fn validate_config(config: &Config) -> anyhow::Result<()> {
    anyhow::ensure!(!config.bulk.endpoints.is_empty(), ...);
    anyhow::ensure!(config.bulk.signing_key_path.exists(), ...);
    ...
    Ok(())
}
``` [2](#0-1) 

The `access_token` is passed directly to `PythLazerStreamClientBuilder` and used to authenticate the WebSocket connection, regardless of whether the scheme is `wss://` or `ws://`: [3](#0-2) 

The same pattern exists in `price_service/client/js/src/utils.ts`, where `makeWebsocketUrl` silently converts `http://` to `ws://` with no warning:

```typescript
export function makeWebsocketUrl(endpoint: string) {
  const url = new URL("ws", endpoint);
  const useHttps = url.protocol === "https:";
  url.protocol = useHttps ? "wss:" : "ws:";
  return url.toString();
}
``` [4](#0-3) 

When an `accessToken` is provided to `PriceServiceConnection`, it is appended as a plaintext `ACCESS_TOKEN` query parameter to the resulting `ws://` URL: [5](#0-4) 

By contrast, `apps/mcp/src/config.ts` demonstrates the correct pattern — it enforces HTTPS via a Zod `.refine()` check:

```typescript
.refine((u) => u.startsWith("https://"), "URL must use HTTPS"),
``` [6](#0-5) 

This mitigation is absent from `LazerConfig` and the Hermes client configurations.

---

### Impact Explanation

If a Lazer pusher operator configures `ws://` endpoints (e.g., pointing to an internal or external Lazer router over an unencrypted channel), an attacker on the same network segment can:

1. **Steal the `access_token`** — the credential is transmitted in plaintext over the unencrypted WebSocket handshake, allowing the attacker to authenticate as the pusher to the Lazer router.
2. **Inject tampered price data** — by acting as a MITM proxy, the attacker can modify `StreamUpdated` messages in transit, causing the pusher to cache and forward incorrect prices to downstream systems (e.g., Bulk Trade validators). [7](#0-6) 

The downstream effect is that Bulk Trade validators receive and act on attacker-controlled prices, potentially enabling market manipulation or financial loss for users of those systems.

---

### Likelihood Explanation

The likelihood is **medium**. The default example configuration (`config.example.toml`) uses `wss://`, but there is no enforcement in code. Operators deploying in internal networks, behind load balancers, or in development environments commonly use `ws://` without realizing the credential and data exposure risk. The absence of any warning or rejection makes this a silent misconfiguration. [8](#0-7) 

---

### Recommendation

1. **Short term**: Add scheme validation in `validate_config` (and equivalently in `LazerConfig` deserialization) to reject or warn on non-`wss://` endpoints when the host is not `localhost`/`127.0.0.1`:

```rust
for endpoint in &config.base.lazer.endpoints {
    if endpoint.scheme() != "wss" {
        tracing::warn!("Lazer endpoint {} uses unencrypted scheme; access_token will be transmitted in plaintext", endpoint);
    }
}
```

2. **Long term**: Apply the same `.refine()` pattern used in `apps/mcp/src/config.ts` to all endpoint configuration structs (`LazerConfig`, `BulkConfig`, Hermes client builder) to reject non-TLS schemes for non-local hosts at parse time. [6](#0-5) 

---

### Proof of Concept

```toml
# config.toml — operator misconfigures with ws:// (e.g., internal network, dev environment)
[lazer]
endpoints = ["ws://192.168.1.50/v1/stream"]
access_token = "secret-lazer-api-key"
num_connections = 1
```

1. Operator starts `bulk-pusher` with the above config. `validate_config` passes without error.
2. Attacker on the same LAN runs a MITM proxy (e.g., `mitmproxy`) between the pusher and `192.168.1.50`.
3. The WebSocket upgrade request contains `Authorization: Bearer secret-lazer-api-key` in plaintext — attacker captures the token.
4. Attacker modifies `StreamUpdated` JSON messages in transit, replacing real prices with attacker-chosen values.
5. The pusher's `process_lazer_updates` function caches the tampered prices and the `pusher.rs` loop forwards them to Bulk Trade validators. [9](#0-8) [2](#0-1)

### Citations

**File:** apps/pyth-lazer-pusher/pusher-base/src/lazer/config.rs (L11-26)
```rust
pub struct LazerConfig {
    /// Router WebSocket endpoints
    pub endpoints: Vec<Url>,

    /// Access token for authentication
    #[derivative(Debug = "ignore")]
    pub access_token: String,

    /// Number of WebSocket connections to maintain
    #[serde(default = "default_num_connections")]
    pub num_connections: usize,

    /// Connection timeout
    #[serde(with = "humantime_serde", default = "default_timeout")]
    pub timeout: Duration,
}
```

**File:** apps/pyth-lazer-pusher/bulk-trade-pusher/src/config.rs (L62-88)
```rust
fn validate_config(config: &Config) -> anyhow::Result<()> {
    // Validate bulk endpoints
    anyhow::ensure!(
        !config.bulk.endpoints.is_empty(),
        "bulk.endpoints cannot be empty - at least one validator endpoint is required"
    );

    // Validate signing key file exists
    anyhow::ensure!(
        config.bulk.signing_key_path.exists(),
        "bulk.signing_key_path does not exist: {}",
        config.bulk.signing_key_path.display()
    );

    // Validate oracle account is not empty
    anyhow::ensure!(
        !config.bulk.oracle_account_pubkey_base58.is_empty(),
        "bulk.oracle_account_pubkey_base58 cannot be empty"
    );

    // Validate feed subscriptions
    anyhow::ensure!(
        !config.base.feeds.subscriptions.is_empty(),
        "feeds.subscriptions cannot be empty - at least one feed subscription is required"
    );

    Ok(())
```

**File:** apps/pyth-lazer-pusher/pusher-base/src/lazer/receiver.rs (L63-68)
```rust
        let mut lazer_client = PythLazerStreamClientBuilder::new(lazer_config.access_token.clone())
            .with_endpoints(lazer_config.endpoints.clone())
            .with_num_connections(lazer_config.num_connections)
            .with_timeout(lazer_config.timeout)
            .build()
            .context("failed to build Lazer client")?;
```

**File:** apps/pyth-lazer-pusher/pusher-base/src/lazer/receiver.rs (L190-242)
```rust
        match response {
            AnyResponse::Json(ws_response) => {
                match ws_response {
                    WsResponse::Error(error) => {
                        error!("Lazer websocket error: {:?}", error);
                    }
                    WsResponse::Subscribed(subscribed) => {
                        debug!("Lazer subscription successful: {:?}", subscribed);
                    }
                    WsResponse::SubscribedWithInvalidFeedIdsIgnored(subscribe_invalid) => {
                        warn!(
                            "Lazer subscription with invalid feed ids ignored: {:?}",
                            subscribe_invalid
                        );
                    }
                    WsResponse::Unsubscribed(unsubscribed) => {
                        warn!(
                            "Lazer subscription unexpectedly unsubscribed: {:?}",
                            unsubscribed
                        );
                    }
                    WsResponse::SubscriptionError(subscription_error) => {
                        error!("Lazer subscription error: {:?}", subscription_error);
                    }
                    WsResponse::StreamUpdated(update) => {
                        // Process parsed payload
                        if let Some(parsed) = update.payload.parsed {
                            // Convert timestamp from microseconds to milliseconds
                            let timestamp_ms = parsed.timestamp_us.as_millis();

                            for feed_payload in parsed.price_feeds {
                                let feed_id = feed_payload.price_feed_id;

                                if feed_registry.has_feed(feed_id) {
                                    // Record metrics if available
                                    if let Some(ref m) = metrics {
                                        m.record_lazer_update(feed_id.0);
                                    }

                                    // Update cache
                                    let cached = CachedPrice {
                                        data: feed_payload,
                                        timestamp_ms,
                                        feed_id,
                                    };

                                    let mut cache = price_cache.write().await;
                                    cache.insert(feed_id, cached);
                                }
                            }
                        }
                    }
                }
```

**File:** price_service/client/js/src/utils.ts (L9-15)
```typescript
export function makeWebsocketUrl(endpoint: string) {
  const url = new URL("ws", endpoint);
  const useHttps = url.protocol === "https:";

  url.protocol = useHttps ? "wss:" : "ws:";

  return url.toString();
```

**File:** price_service/client/js/src/PriceServiceConnection.ts (L160-168)
```typescript
    this.wsEndpoint = makeWebsocketUrl(endpoint);

    // Append access token as query param for WebSocket connections
    // since browser WebSocket API does not support custom headers.
    if (this.accessToken && this.wsEndpoint) {
      const wsUrl = new URL(this.wsEndpoint);
      wsUrl.searchParams.append("ACCESS_TOKEN", this.accessToken);
      this.wsEndpoint = wsUrl.toString();
    }
```

**File:** apps/mcp/src/config.ts (L7-18)
```typescript
  historyUrl: z
    .string()
    .url()
    .default("https://history.pyth-lazer.dourolabs.app")
    .refine((u) => u.startsWith("https://"), "URL must use HTTPS"),
  logLevel: z.enum(["debug", "info", "warn", "error"]).default("info"),
  requestTimeoutMs: z.coerce.number().int().positive().default(10_000),
  routerUrl: z
    .string()
    .url()
    .default("https://pyth-lazer.dourolabs.app")
    .refine((u) => u.startsWith("https://"), "URL must use HTTPS"),
```

**File:** apps/pyth-lazer-pusher/bulk-trade-pusher/config.example.toml (L19-22)
```text
endpoints = [
    "wss://pyth-lazer-0.dourolabs.app/v1/stream",
    "wss://pyth-lazer-1.dourolabs.app/v1/stream",
]
```
