Audit Report

## Title
RPC Batch Handler Has No Default Limit, Allowing Local Process to Exhaust RPC Resources via Oversized Batch Requests — (`rpc/src/server.rs`)

## Summary
The CKB JSON-RPC server's batch handler enforces a call-count limit only when `rpc_batch_limit` is explicitly set in `ckb.toml`. Because the field is `Option<usize>` with no default, the `OnceLock<usize>` is never populated in a default deployment, and the guard at `server.rs:275` is never triggered. A local process can submit a single HTTP POST containing tens of thousands of RPC calls, forcing sequential processing of all of them and making the RPC endpoint unresponsive to other callers.

## Finding Description
In `rpc/src/server.rs` line 34, `JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` initialized empty. It is only populated at lines 53–55 when `config.rpc_batch_limit` is `Some`:

```rust
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
```

The batch guard at lines 274–282 uses `JSONRPC_BATCH_LIMIT.get()`, which returns `None` when the lock is unpopulated, so the entire `if let` block is skipped and all calls are processed unconditionally:

```rust
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    { ... }
    // falls through with no bound
```

In `util/app-config/src/configs/rpc.rs` line 44, the field is `pub rpc_batch_limit: Option<usize>` with no `serde(default)`. In `resource/ckb.toml` lines 205–208, the setting is commented out with an explicit note that there is no default limit. The only existing bound is `max_request_body_size` (10 MiB), which permits roughly 174,000 minimal calls per request. The test setup in `rpc/src/tests/setup.rs` line 184 explicitly sets `rpc_batch_limit: Some(1000)`, confirming the production default is `None`.

## Impact Explanation
An unprivileged local process can send a single oversized batch request that occupies the RPC server's async executor for the full 30-second `TimeoutLayer` window, processing tens of thousands of sequential RPC calls. During this window, legitimate RPC callers receive no responses. This constitutes a local RPC API crash/denial-of-service, matching the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**. The node's P2P and consensus subsystems are unaffected; only the RPC endpoint is impacted. The claim's assertion of High-level node crash is not supported — the node process itself does not terminate.

## Likelihood Explanation
The default `ckb.toml` ships with `rpc_batch_limit` commented out. Any operator who does not explicitly add the setting is affected. The RPC binds to `127.0.0.1:8114` by default, so the attacker must be a local process or a service co-located on the same host. No authentication, credentials, or special privileges are required — only the ability to make an HTTP POST to the loopback interface. The attack is trivially repeatable and requires no prior knowledge of the node state.

## Recommendation
Add a non-optional default to `util/app-config/src/configs/rpc.rs`:

```rust
#[serde(default = "default_rpc_batch_limit")]
pub rpc_batch_limit: usize,

fn default_rpc_batch_limit() -> usize { 2000 }
```

Remove the `Option` wrapper and update `server.rs` to always enforce the limit directly, eliminating the `OnceLock` indirection. Update `resource/ckb.toml` to document the active default rather than a commented-out suggestion.

## Proof of Concept
```bash
# Generate a batch of 50,000 calls (~3.5 MB, within the 10 MiB body limit)
python3 -c "
import json
calls = [{'jsonrpc':'2.0','method':'get_tip_block_number','params':[],'id':i} for i in range(50000)]
print(json.dumps(calls))
" > batch.json

# Against a default CKB node with no rpc_batch_limit configured
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  --data @batch.json > /dev/null &

# Concurrent connections amplify the effect
for i in $(seq 1 8); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    --data @batch.json > /dev/null &
done
wait
# RPC endpoint is unresponsive to legitimate callers during the 30s window
```

The existing unit test `test_rpc_batch_request_limit` in `rpc/src/tests/module/test.rs` lines 109–132 already validates enforcement at 1000 calls, but the test setup hard-codes `rpc_batch_limit: Some(1000)` — it does not test the production default of `None`.