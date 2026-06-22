### Title
Unbounded JSON-RPC Batch Request Processing Exhausts Node Resources by Default - (File: `rpc/src/server.rs`)

---

### Summary

The CKB JSON-RPC server ships with **no default limit on JSON-RPC batch request size**. The `rpc_batch_limit` configuration option is explicitly disabled by default. Any caller with access to the RPC port can submit a single HTTP POST containing an arbitrarily large JSON-RPC batch array, forcing the node to process every call sequentially with no cap, exhausting CPU, memory, and the Tokio runtime thread pool.

---

### Finding Description

In `rpc/src/server.rs`, the global batch limit is stored in a `OnceLock`:

```rust
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();
```

It is only initialized if the operator explicitly sets `rpc_batch_limit` in the config:

```rust
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
``` [1](#0-0) 

The default configuration explicitly leaves this unset:

```toml
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
``` [2](#0-1) 

In `handle_jsonrpc`, the batch guard uses `JSONRPC_BATCH_LIMIT.get()`, which returns `None` when the limit was never initialized. The `if let Some(...)` short-circuits and the entire check is skipped:

```rust
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    {
        return make_error_response(...);
    }
    // ALL calls processed with no limit
    let stream = stream::iter(calls)
        .then(move |call| { ... io.handle_call(call, ...) ... })
``` [3](#0-2) 

The `rpc_batch_limit` field in the config struct is typed `Option<usize>` with no default value, confirming the opt-in nature: [4](#0-3) 

---

### Impact Explanation

An attacker with access to the RPC port submits a single HTTP POST containing a JSON-RPC batch of, e.g., 100,000 calls to expensive methods such as `send_transaction` (triggers script verification via CKB-VM), `get_transaction_proof` (Merkle tree computation), or `get_block` (large serialization). The node processes all calls sequentially on the RPC Tokio runtime, blocking legitimate requests, exhausting memory holding all pending responses, and potentially saturating the tx-pool or chain service channels. The node becomes unresponsive to miners, peers relaying blocks, and other RPC clients.

---

### Likelihood Explanation

The RPC default bind address is `127.0.0.1:8114`, but operators routinely expose it to LAN or public networks for dApp development, exchange integrations, and tooling — the very use case the docs warn about but cannot prevent. The `rpc_batch_limit` is opt-in and commented out in the shipped default config, meaning the vast majority of deployments are unprotected. No authentication, token, or proof-of-work is required to submit a batch request. A single HTTP connection is sufficient. [5](#0-4) 

---

### Recommendation

1. **Set a safe default for `rpc_batch_limit`** (e.g., 200) in the `Config` struct using `#[serde(default = "default_batch_limit")]` so it is enforced even when the operator does not configure it explicitly.
2. **Make the limit mandatory** rather than `Option<usize>` — operators who need larger batches can raise it, but the default must be bounded.
3. Consider adding per-IP or per-connection request rate limiting at the HTTP layer (e.g., via a Tower middleware) for the HTTP and WebSocket servers.
4. The TCP subscription server (`start_tcp_server`) also spawns an unbounded number of `tokio::spawn` tasks per accepted connection with no connection counter — a connection limit should be added there as well. [6](#0-5) 

---

### Proof of Concept

```python
import json, socket, requests

# Build a batch of 50,000 send_transaction calls (or any expensive method)
batch = [
    {
        "id": i,
        "jsonrpc": "2.0",
        "method": "get_block_by_number",
        "params": ["0x0"]
    }
    for i in range(50000)
]

# Single HTTP POST — no authentication required
r = requests.post(
    "http://<node-ip>:8114",
    json=batch,
    timeout=None
)
# Node is now busy processing 50,000 calls sequentially.
# Legitimate RPC callers (miners, relayers) receive no response.
```

With `rpc_batch_limit` unset (the default), `JSONRPC_BATCH_LIMIT.get()` returns `None`, the guard at line 275 of `rpc/src/server.rs` is skipped, and all 50,000 calls are dispatched. Substituting `send_transaction` with crafted transactions triggers full CKB-VM script verification per call, amplifying CPU cost by orders of magnitude. [7](#0-6)

### Citations

**File:** rpc/src/server.rs (L53-55)
```rust
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }
```

**File:** rpc/src/server.rs (L175-194)
```rust
                _ = async {
                        while let Ok((stream, _)) = listener.accept().await {
                            let rpc = Arc::clone(&rpc);
                            let stream_config = stream_config.clone();
                            let codec = codec.clone();
                            tokio::spawn(async move {
                                let (r, w) = stream.into_split();
                                let r = FramedRead::new(r, codec.clone()).map_ok(StreamMsg::Str);
                                let w = FramedWrite::new(w, codec).with(|msg| async move {
                                    Ok::<_, LinesCodecError>(match msg {
                                        StreamMsg::Str(msg) => msg,
                                        _ => "".into(),
                                    })
                                });
                                tokio::pin!(w);
                                if let Err(err) = serve_stream_sink(&rpc, w, r, stream_config).await {
                                    info!("TCP RPCServer error: {:?}", err);
                                }
                            });
                        }
```

**File:** rpc/src/server.rs (L274-296)
```rust
            Request::Batch(calls) => {
                if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
                    && calls.len() > *batch_size
                {
                    return make_error_response(jsonrpc_core::Error::invalid_params(format!(
                        "batch size is too large, expect it less than: {}",
                        batch_size
                    )));
                }

                let stream = stream::iter(calls)
                    .then(move |call| {
                        let io = Arc::clone(&io);
                        async move { io.handle_call(call, T::default()).await }
                    })
                    .filter_map(|response| async move { response });

                (
                    [(axum::http::header::CONTENT_TYPE, "application/json")],
                    StreamBodyAs::json_array(stream),
                )
                    .into_response()
            }
```

**File:** resource/ckb.toml (L177-182)
```text
[rpc]
# By default RPC only binds to localhost, thus it only allows accessing from the same machine.
#
# Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged.
# Please strictly limit the access to only trusted machines.
listen_address = "127.0.0.1:8114" # {{
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

**File:** util/app-config/src/configs/rpc.rs (L43-44)
```rust
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
```
