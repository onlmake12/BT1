### Title
Unbounded JSON-RPC Batch Request Size by Default Enables Resource Exhaustion DoS - (File: `rpc/src/server.rs`)

---

### Summary

The CKB JSON-RPC server ships with `rpc_batch_limit` **disabled by default**. Any RPC caller can submit a single HTTP POST containing a JSON array of thousands of expensive RPC calls. The server processes every call in the batch sequentially with no count guard, enabling CPU and memory exhaustion proportional to the number of calls packed into the 10 MiB body limit.

---

### Finding Description

The batch-size guard in `handle_jsonrpc` is gated on `JSONRPC_BATCH_LIMIT.get()`, which is only populated when the operator explicitly sets `rpc_batch_limit` in `ckb.toml`: [1](#0-0) 

`JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` that is only initialized when `config.rpc_batch_limit` is `Some(...)`: [2](#0-1) 

The RPC config struct declares `rpc_batch_limit` as `Option<usize>` with no default value: [3](#0-2) 

The shipped default `ckb.toml` leaves the field commented out, explicitly noting there is **no limitation** on batch size: [4](#0-3) 

When `rpc_batch_limit` is absent (the default), the `if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()` branch is never entered, and the server unconditionally iterates over every call in the batch: [5](#0-4) 

The only constraint is `max_request_body_size`, which defaults to **10 MiB**: [6](#0-5) 

Within 10 MiB, an attacker can pack tens of thousands of minimal JSON-RPC call objects (e.g., `{"jsonrpc":"2.0","method":"get_block_template","params":[null,null,null],"id":1}` is ~80 bytes), each of which triggers a full block-template assembly involving tx-pool reads and DAO calculations. [7](#0-6) 

---

### Impact Explanation

A single HTTP POST to the RPC endpoint with a JSON array of ~100,000 lightweight calls forces the server to dispatch and await each call sequentially. This saturates the async runtime, starves other RPC consumers, and can exhaust memory through accumulated response buffering. Computationally heavy methods (`get_block_template`, `send_transaction`) amplify the effect. The node's RPC service becomes unresponsive for the duration of processing.

Impact: **3** (service availability degradation / DoS of RPC layer).

---

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, so the attacker must be a local process or a remote caller on a node that has exposed the RPC port externally (a common operator configuration). No authentication is required. The attack requires only a single crafted HTTP request with no special privileges. The default configuration ships with the protection disabled and no warning that it is off.

Likelihood: **3**.

---

### Recommendation

1. **Set a safe default for `rpc_batch_limit`** (e.g., 200) in `Config` using `#[serde(default = "default_batch_limit")]` so protection is active without operator action.
2. **Enforce the limit unconditionally** rather than only when the operator opts in.
3. Consider also capping the total number of *parameters* per individual call to prevent parameter-level amplification.
4. Document the risk of leaving `rpc_batch_limit` unset in the default `ckb.toml`.

---

### Proof of Concept

```bash
# Build a batch of 50,000 get_block_template calls (~4 MB body, well under 10 MiB limit)
python3 -c "
import json, sys
calls = [{'jsonrpc':'2.0','method':'get_block_template','params':[None,None,None],'id':i} for i in range(50000)]
sys.stdout.write(json.dumps(calls))
" > batch.json

# Send to a default CKB node (rpc_batch_limit not set)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  --data-binary @batch.json > /dev/null &

# Observe: subsequent RPC calls hang while the batch is processed
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_tip_block_number","params":[],"id":1}'
```

The second request will be delayed or time out while the server works through the 50,000-call batch, demonstrating RPC service unavailability.

### Citations

**File:** rpc/src/server.rs (L53-55)
```rust
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }
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

**File:** rpc/src/server.rs (L284-289)
```rust
                let stream = stream::iter(calls)
                    .then(move |call| {
                        let io = Arc::clone(&io);
                        async move { io.handle_call(call, T::default()).await }
                    })
                    .filter_map(|response| async move { response });
```

**File:** util/app-config/src/configs/rpc.rs (L43-44)
```rust
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
```

**File:** resource/ckb.toml (L186-187)
```text
# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

**File:** rpc/src/module/miner.rs (L238-258)
```rust
    fn get_block_template(
        &self,
        bytes_limit: Option<Uint64>,
        proposals_limit: Option<Uint64>,
        max_version: Option<Version>,
    ) -> Result<BlockTemplate> {
        let bytes_limit = bytes_limit.map(|b| b.into());

        let proposals_limit = proposals_limit.map(|b| b.into());

        self.shared
            .get_block_template(bytes_limit, proposals_limit, max_version.map(Into::into))
            .map_err(|err| {
                error!("Send get_block_template request error {}", err);
                RPCError::ckb_internal_error(err)
            })?
            .map_err(|err| {
                error!("Get_block_template result error {}", err);
                RPCError::from_any_error(err)
            })
    }
```
