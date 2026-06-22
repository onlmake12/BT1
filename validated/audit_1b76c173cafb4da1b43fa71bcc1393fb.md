### Title
Miner RPC Client Discards HTTP Status Code and Applies No Backoff on Repeated Errors — (File: `miner/src/client.rs`)

---

### Summary

The `Rpc` HTTP client used by the CKB miner unconditionally discards the HTTP response status code before attempting to parse the response body. If the RPC endpoint returns a non-200 status (e.g., `429 Too Many Requests` or `503 Service Unavailable` from a reverse proxy or an overloaded node), the miner silently treats it as a JSON parse error and continues polling at the same fixed rate with no backoff, no delay increase, and no circuit-breaking. This is the direct CKB analog of the price-feeder continuing to spam an API after receiving a `429`.

---

### Finding Description

In `Rpc::new()`, every HTTP response is mapped to its body immediately after `await`, discarding the status code entirely:

```rust
let request = match client
    .request(req)
    .await
    .map(|res| res.into_body())   // status code thrown away here
{
    Ok(body) => BodyExt::collect(body).await
                    .map_err(RpcError::Http)
                    .map(|t| t.to_bytes()),
    Err(err) => Err(RpcError::Client(err)),
};
``` [1](#0-0) 

The body bytes are then unconditionally fed to `serde_json::from_slice`, which fails with `RpcError::Json` for any non-JSON body (e.g., an HTML `429` error page): [2](#0-1) 

`fetch_block_template()` inspects only `RpcError::Fail(MethodNotFound)` and logs everything else as a generic error — no backoff, no delay, no state change: [3](#0-2) 

`poll_block_template()` drives the polling loop at a fixed `poll_interval` (default 1 000 ms) regardless of how many consecutive errors have occurred: [4](#0-3) 

The internal mpsc channel is sized at 65 535 slots, so a slow or error-returning endpoint allows a large backlog of in-flight spawned tasks to accumulate before any natural back-pressure appears: [5](#0-4) 

The `RpcError` enum has no variant for an HTTP-level status code, so the information is structurally unrepresentable and cannot be acted upon anywhere in the call chain: [6](#0-5) 

---

### Impact Explanation

A miner operator who routes the RPC connection through any rate-limiting reverse proxy (nginx `limit_req`, Cloudflare, AWS ALB, etc.) will have the miner silently hammering the endpoint at full speed after the proxy starts returning `429`. Because the miner cannot distinguish a `429` body from a network error, it never backs off. The proxy may escalate to a temporary or permanent block of the miner's source address, causing the miner to stop receiving block templates and stop submitting solved blocks. The miner loses all mining rewards for the duration of the block. This is a direct financial impact on a supported local CLI/RPC user (`miner/block-template caller`) within the stated bounty scope.

---

### Likelihood Explanation

Placing a reverse proxy in front of the CKB RPC port is a common production hardening practice (the default config explicitly warns that exposing RPC to arbitrary machines is dangerous and recommends access controls). [7](#0-6) 

Any such proxy that enforces a per-IP or per-second request rate will trigger this path. The default `poll_interval` of 1 000 ms means 1 RPC call per second under normal conditions, but a miner with multiple workers or a misconfigured short interval can easily exceed common proxy thresholds. The bug is always present; it requires only a rate-limiting intermediary to become exploitable.

---

### Recommendation

1. **Short term:** After `client.request(req).await`, check `res.status()` before calling `res.into_body()`. Map `429` and `503` to a dedicated `RpcError::RateLimited` / `RpcError::ServiceUnavailable` variant. In `fetch_block_template()`, detect these variants and apply an exponential backoff (e.g., double the sleep up to a configurable maximum) before the next poll tick.

2. **Long term:** Add a configurable `max_poll_interval` and `backoff_multiplier` to `ClientConfig` so operators can tune retry behaviour. Expose a metric counter for consecutive RPC errors so operators can alert on sustained failures before they result in missed blocks.

---

### Proof of Concept

1. Start a CKB node with the Miner RPC module enabled.
2. Place nginx in front of port 8114 with `limit_req zone=rpc burst=2 nodelay; limit_req_zone $binary_remote_addr zone=rpc:1m rate=1r/s;`.
3. Start `ckb miner` with `poll_interval = 200` (5 req/s).
4. Observe nginx logs: after the burst is consumed, nginx returns `429` for every subsequent request.
5. Observe miner logs: the miner logs `rpc call get_block_template error: Json(...)` on every tick but never slows down — it continues at 5 req/s indefinitely.
6. Tighten nginx to `deny` after N consecutive `429`s (or use `fail2ban`): the miner is now permanently blocked and stops producing new work, halting block submission.

The root cause — `map(|res| res.into_body())` discarding the status — is confirmed at: [1](#0-0)

### Citations

**File:** miner/src/client.rs (L44-52)
```rust
pub enum RpcError {
    Http(HyperError),
    Client(ClientError),
    Canceled, //oneshot canceled
    Json(JsonError),
    Fail(RpcFail),
    SendError,
    NoRespData,
}
```

**File:** miner/src/client.rs (L61-61)
```rust
        let (sender, mut receiver) = mpsc::channel(65_535);
```

**File:** miner/src/client.rs (L84-91)
```rust
                            let request = match client
                                .request(req)
                                .await
                                .map(|res|res.into_body())
                            {
                                Ok(body) => BodyExt::collect(body).await.map_err(RpcError::Http).map(|t| t.to_bytes()),
                                Err(err) => Err(RpcError::Client(err)),
                            };
```

**File:** miner/src/client.rs (L131-134)
```rust
            rev.map_err(|_| RpcError::Canceled)
                .await?
                .and_then(|chunk| serde_json::from_slice(&chunk).map_err(RpcError::Json))
        }
```

**File:** miner/src/client.rs (L273-291)
```rust
    async fn poll_block_template(&self) {
        let poll_interval = time::Duration::from_millis(self.config.poll_interval);
        let mut interval = tokio::time::interval(poll_interval);
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let stop_rx: CancellationToken = new_tokio_exit_rx();
        loop {
            tokio::select! {
                _ = interval.tick() => {
                    debug!("poll block template...");
                    self.fetch_block_template().await;
                }
                _ = stop_rx.cancelled() => {
                    info!("Miner client pool_block_template received exit signal, exit now");
                    break
                },
                else => break,
            }
        }
    }
```

**File:** miner/src/client.rs (L318-342)
```rust
    async fn fetch_block_template(&self) {
        match self.get_block_template().await {
            Ok(block_template) => {
                self.update_block_template(block_template);
            }
            Err(ref err) => {
                let is_method_not_found = if let RpcError::Fail(RpcFail { code, .. }) = err {
                    *code == RpcFailCode::MethodNotFound
                } else {
                    false
                };
                if is_method_not_found {
                    error!(
                        "RPC Method Not Found: \
                         Please perform the following checks: \
                         1. Ensure that the CKB server has enabled the Miner API module; \
                         2. Verify that the CKB server has set the `block_assembler` correctly; \
                         3. Confirm that the RPC URL for CKB miner is correct.",
                    );
                } else {
                    error!("rpc call get_block_template error: {:?}", err);
                }
            }
        }
    }
```

**File:** resource/ckb.toml (L178-183)
```text
# By default RPC only binds to localhost, thus it only allows accessing from the same machine.
#
# Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged.
# Please strictly limit the access to only trusted machines.
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
```
