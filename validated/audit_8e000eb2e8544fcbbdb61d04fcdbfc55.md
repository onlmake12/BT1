### Title
Unbounded `ban_reason` String via `set_ban` RPC Causes Memory Exhaustion — (`rpc/src/server.rs`, `rpc/src/module/net.rs`, `network/src/peer_store/ban_list.rs`)

---

### Summary

The `set_ban` RPC method accepts an optional `reason` string with no length validation. The `max_request_body_size` config field exists but is **never wired into the HTTP server**, so the axum router accepts arbitrarily large HTTP bodies. A caller with RPC access can store a multi-hundred-megabyte string in `BanList::inner`, which is then cloned and serialized on every `get_banned_addresses()` call.

---

### Finding Description

**Step 1 — HTTP body limit is declared but never enforced.**

`RpcConfig` declares `max_request_body_size`: [1](#0-0) 

But `start_server()` builds the axum `Router` without any `RequestBodyLimitLayer` or equivalent middleware: [2](#0-1) 

The `handle_jsonrpc` handler receives `req_body: Bytes` directly from axum with no size guard: [3](#0-2) 

By contrast, the TCP server correctly applies a 2 MB codec limit: [4](#0-3) 

**Step 2 — `set_ban` passes `reason` through without any length check.** [5](#0-4) 

The `reason` string is forwarded verbatim to `NetworkController::ban()`, then to `PeerStore::ban_network()`: [6](#0-5) 

**Step 3 — `BannedAddr::ban_reason` is an unbounded `String` stored in the in-memory `HashMap`.** [7](#0-6) [8](#0-7) 

**Step 4 — `get_banned_addrs()` clones every entry, including the huge string.** [9](#0-8) 

`get_banned_addresses()` then maps each clone into a JSON-serializable type and returns it: [10](#0-9) 

---

### Impact Explanation

An attacker with HTTP RPC access sends a `set_ban` call with a `reason` of, say, 200 MB. The node allocates that string in `BanList::inner`. Every subsequent `get_banned_addresses()` call (by any caller, including monitoring scripts) clones the full string and serializes it into the HTTP response, doubling peak memory usage per call. Repeated calls or multiple large entries can exhaust available RAM and OOM-kill the node process, taking it off the network.

---

### Likelihood Explanation

The RPC is unauthenticated. While it defaults to `127.0.0.1`, many operators expose it on a LAN or behind a reverse proxy. Any process or user with TCP access to the RPC port can trigger this with a single HTTP POST. No PoW, no key, no privileged role is required.

---

### Recommendation

1. **Enforce `max_request_body_size` in the HTTP server.** Add `tower_http::limit::RequestBodyLimitLayer::new(config.max_request_body_size)` to the axum router in `start_server()`.
2. **Validate `ban_reason` length in `set_ban`.** Reject reasons exceeding a small constant (e.g., 256 bytes) with `RPCError::invalid_params`.
3. **Cap `BannedAddr::ban_reason` at construction** in `ban_network()` or `BanList::ban()` as a defense-in-depth measure.

---

### Proof of Concept

```python
import requests, json

# 100 MB reason string
huge_reason = "A" * 100_000_000

payload = {
    "id": 1,
    "jsonrpc": "2.0",
    "method": "set_ban",
    "params": ["192.168.0.2", "insert", None, None, huge_reason]
}

# Step 1: store the huge string
requests.post("http://127.0.0.1:8114/", json=payload)

# Step 2: each call clones + serializes 100 MB
for _ in range(10):
    r = requests.post("http://127.0.0.1:8114/", json={
        "id": 2, "jsonrpc": "2.0",
        "method": "get_banned_addresses", "params": []
    })
    assert len(r.content) > 100_000_000
```

### Citations

**File:** util/app-config/src/configs/rpc.rs (L40-40)
```rust
    pub max_request_body_size: usize,
```

**File:** rpc/src/server.rs (L119-129)
```rust
        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));
```

**File:** rpc/src/server.rs (L165-165)
```rust
            let codec = LinesCodec::new_with_max_length(2 * 1024 * 1024);
```

**File:** rpc/src/server.rs (L218-221)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
```

**File:** rpc/src/module/net.rs (L672-684)
```rust
    fn get_banned_addresses(&self) -> Result<Vec<BannedAddr>> {
        Ok(self
            .network_controller
            .get_banned_addrs()
            .into_iter()
            .map(|banned| BannedAddr {
                address: banned.address.to_string(),
                ban_until: banned.ban_until.into(),
                ban_reason: banned.ban_reason,
                created_at: banned.created_at.into(),
            })
            .collect())
    }
```

**File:** rpc/src/module/net.rs (L691-717)
```rust
    fn set_ban(
        &self,
        address: String,
        command: String,
        ban_time: Option<Timestamp>,
        absolute: Option<bool>,
        reason: Option<String>,
    ) -> Result<()> {
        let ip_network = address.parse().map_err(|_| {
            RPCError::invalid_params(format!(
                "Expected `params[0]` to be a valid IP address, got {address}"
            ))
        })?;

        match command.as_ref() {
            "insert" => {
                let ban_until = if absolute.unwrap_or(false) {
                    ban_time.unwrap_or_default().into()
                } else {
                    unix_time_as_millis()
                        + ban_time
                            .unwrap_or_else(|| DEFAULT_BAN_DURATION.into())
                            .value()
                };
                self.network_controller
                    .ban(ip_network, ban_until, reason.unwrap_or_default());
                Ok(())
```

**File:** network/src/peer_store/peer_store_impl.rs (L294-303)
```rust
    pub(crate) fn ban_network(&mut self, network: IpNetwork, timeout_ms: u64, ban_reason: String) {
        let now_ms = ckb_systemtime::unix_time_as_millis();
        let ban_addr = BannedAddr {
            address: network,
            ban_until: now_ms + timeout_ms,
            created_at: now_ms,
            ban_reason,
        };
        self.mut_ban_list().ban(ban_addr);
    }
```

**File:** network/src/peer_store/types.rs (L127-137)
```rust
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Hash)]
pub struct BannedAddr {
    /// Ip address
    pub address: IpNetwork,
    /// Ban until time
    pub ban_until: u64,
    /// Ban reason
    pub ban_reason: String,
    /// Ban time
    pub created_at: u64,
}
```

**File:** network/src/peer_store/ban_list.rs (L13-16)
```rust
pub struct BanList {
    inner: HashMap<IpNetwork, BannedAddr>,
    insert_count: usize,
}
```

**File:** network/src/peer_store/ban_list.rs (L75-77)
```rust
    pub fn get_banned_addrs(&self) -> Vec<BannedAddr> {
        self.inner.values().map(ToOwned::to_owned).collect()
    }
```
