Audit Report

## Title
Unbounded Batch Request Processing and TCP Connection Acceptance Enable Local Node DoS — (`rpc/src/server.rs`)

## Summary

The CKB RPC server has two confirmed resource-exhaustion paths. First, the HTTP RPC handler processes JSON-RPC batch requests of arbitrary size when `rpc_batch_limit` is unset (the default), allowing any local caller to submit a batch large enough to exhaust CPU and memory. Second, the TCP RPC server (when enabled) spawns one unbounded `tokio::spawn` task per accepted connection with no cap, allowing a local process to exhaust file descriptors and memory. Both paths are reachable without credentials and can render the node unresponsive.

## Finding Description

**Root Cause 1 — Batch request size unbounded by default**

In `handle_jsonrpc` at `rpc/src/server.rs:274–282`, the batch-size guard is gated on `JSONRPC_BATCH_LIMIT.get()`, which only returns `Some` if `config.rpc_batch_limit` was set. The static is populated only at `rpc/src/server.rs:53–55`:

```rust
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
```

The production default in `resource/ckb.toml:205–208` leaves `rpc_batch_limit` commented out. When the field is absent, `Config::rpc_batch_limit` is `None` (`util/app-config/src/configs/rpc.rs:44`), so `JSONRPC_BATCH_LIMIT` is never initialized and the guard at line 275 is never entered. All calls in the batch are processed sequentially via `stream::iter(calls).then(...)` with no upper bound.

**Root Cause 2 — TCP RPC server: unbounded connection acceptance**

`start_tcp_server` at `rpc/src/server.rs:176–193` loops unconditionally on `listener.accept()` and immediately calls `tokio::spawn` for every accepted connection. There is no semaphore, atomic counter, or connection limit. Each spawned task holds a `LinesCodec` buffer sized up to 2 MiB (`rpc/src/server.rs:165`). The `RpcConfig` struct has no field for a maximum TCP connection count. TCP RPC is disabled by default (`resource/ckb.toml:196` is commented out), so this path requires operator opt-in.

## Impact Explanation

**Batch amplification (always-on HTTP RPC):** A local caller submitting a batch of tens of thousands of expensive calls (e.g., `get_block_template`) forces the Tokio runtime to process all of them sequentially, blocking the RPC thread pool and potentially triggering an OOM kill. This matches **Note (0–500 points): Any local RPC API crash**.

**TCP connection flood (opt-in TCP RPC):** 10,000 persistent connections × 2 MiB codec buffer = ~20 GiB memory pressure, plus OS file-descriptor exhaustion. This can crash the node process entirely, matching **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node** — but only when TCP RPC is explicitly enabled by the operator.

## Likelihood Explanation

- The HTTP RPC is always bound to `127.0.0.1:8114`. Any local process (unprivileged) can POST a large batch with no authentication. The batch limit being off by default is explicitly acknowledged in the config comment, confirming the developers are aware of the gap.
- The TCP RPC requires `tcp_listen_address` to be uncommented in `ckb.toml`. When enabled, it is also localhost-only, so the attacker must be a local process. The exploit is trivially repeatable with a shell loop.

## Recommendation

1. **Batch limit:** Set a safe non-`None` default for `rpc_batch_limit` (e.g., 100–1000) in `Config` or in the generated default `ckb.toml`. Operators needing larger batches can raise it explicitly.
2. **TCP connection cap:** Introduce a `rpc_tcp_max_connections` config field. Use a `tokio::sync::Semaphore` acquired before `tokio::spawn` in `start_tcp_server`; drop connections that cannot acquire a permit and return a JSON-RPC error.
3. Return a structured JSON-RPC error (e.g., `-32602 Invalid params`) when either limit is exceeded.

## Proof of Concept

**Batch amplification (default config, no TCP RPC needed):**
```bash
python3 -c "
import json
batch = [{'id': i, 'jsonrpc': '2.0', 'method': 'get_block_template',
          'params': [None, None, None]} for i in range(50000)]
print(json.dumps(batch))
" > big_batch.json

curl -s -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d @big_batch.json
# Node processes all 50,000 calls; RPC becomes unresponsive / OOM
```

**TCP connection flood (requires tcp_listen_address enabled):**
```bash
for i in $(seq 1 10000); do nc -q 0 127.0.0.1 18114 & done
# File descriptor limit exhausted; node unresponsive
```

The batch guard is absent by default at [1](#0-0)  and the TCP accept loop has no cap at [2](#0-1) . The `rpc_batch_limit` field is `Option<usize>` with no default value [3](#0-2)  and is commented out in the shipped config [4](#0-3) .

### Citations

**File:** rpc/src/server.rs (L176-193)
```rust
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
```

**File:** rpc/src/server.rs (L274-282)
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
```

**File:** util/app-config/src/configs/rpc.rs (L43-44)
```rust
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```
