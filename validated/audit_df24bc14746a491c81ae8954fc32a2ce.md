Audit Report

## Title
Unbounded JSON-RPC Batch Request Causes Local RPC Server Resource Exhaustion — (`File: rpc/src/server.rs`)

## Summary
The CKB JSON-RPC server processes batch requests without any enforced default limit on batch size. `JSONRPC_BATCH_LIMIT` is only initialized when `config.rpc_batch_limit` is explicitly set; the shipped default configuration leaves it commented out, meaning the guard is never entered and batches of arbitrary size are accepted. A local process can send a single HTTP POST with tens of thousands of sequential RPC calls within the 10 MiB body limit, saturating the server's processing capacity and making the RPC interface unresponsive.

## Finding Description
In `rpc/src/server.rs`, `JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` initialized only when `config.rpc_batch_limit` is `Some(...)`:

```rust
// Lines 53-55
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
```

When absent, `JSONRPC_BATCH_LIMIT.get()` returns `None`, so the guard at lines 275–282 is never entered:

```rust
// Lines 274-282
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    {
        return make_error_response(...);
    }
    ...
}
```

Batch calls are dispatched **sequentially** via `.then()` (lines 284–289) — each call must complete before the next begins. The only constraint is `max_request_body_size = 10485760` (10 MiB, line 187 of `resource/ckb.toml`). With minimal calls (~50–80 bytes each), an attacker can pack ~130,000–200,000 calls into a single POST. The `TimeoutLayer` at 30 seconds (lines 125–128 of `server.rs`) applies per-request, but multiple concurrent oversized batches can queue indefinitely in the Tokio runtime.

The `rpc_batch_limit` field in `util/app-config/src/configs/rpc.rs` (line 44) is typed `Option<usize>` with no `#[serde(default)]` forcing a value, and `resource/ckb.toml` lines 205–208 explicitly leave it commented out with a comment acknowledging the risk.

## Impact Explanation
A local process can render the node's RPC interface unresponsive for the duration of the attack. This directly maps to the allowed bounty impact: **"Any local RPC API crash" (Note, 0–500 points)**. The RPC endpoint defaults to `127.0.0.1:8114` (localhost only), so the attacker must be a local process. The claim of OOM-induced node process termination is plausible under sustained concurrent flooding but is not concretely proven beyond the sequential saturation case. The primary confirmed impact is local RPC unresponsiveness.

## Likelihood Explanation
The RPC endpoint is localhost-only by default. Any local process — a compromised dependency, a script, or a malicious application running on the same machine — can reach it without credentials. The attack requires only a standard HTTP POST with a JSON array body; no special protocol knowledge is needed. The default configuration ships with the protection disabled, and the config comment itself acknowledges the risk, making the attack straightforward for any local attacker.

## Recommendation
Set a safe hard-coded default for `JSONRPC_BATCH_LIMIT` that applies even when `rpc_batch_limit` is absent from the configuration:

```rust
const DEFAULT_BATCH_LIMIT: usize = 2000;
let limit = config.rpc_batch_limit.unwrap_or(DEFAULT_BATCH_LIMIT);
let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| limit);
```

Additionally, consider replacing the sequential `.then()` dispatch with bounded concurrency (e.g., `buffer_unordered(N)`) so a single slow call does not block the entire batch response stream.

## Proof of Concept
```python
import json, requests

batch = [
    {"jsonrpc": "2.0", "method": "get_tip_block_number", "params": [], "id": i}
    for i in range(100_000)
]
payload = json.dumps(batch)
# ~5.5 MiB — within the 10 MiB max_request_body_size limit

resp = requests.post(
    "http://127.0.0.1:8114",
    data=payload,
    headers={"Content-Type": "application/json"},
    timeout=120,
)
print(resp.status_code)
# While this request is processing, concurrent legitimate RPC calls
# will queue or time out. Repeat with concurrent connections to multiply load.
```