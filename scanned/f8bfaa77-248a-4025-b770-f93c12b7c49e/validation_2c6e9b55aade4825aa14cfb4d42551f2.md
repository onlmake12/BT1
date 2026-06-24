Audit Report

## Title
Unbounded Full-Pool Iteration in `get_raw_tx_pool(verbose=true)` Enables RPC-Triggered Resource Exhaustion — (File: `rpc/src/module/pool.rs`)

## Summary
`get_raw_tx_pool` with `verbose=true` calls `get_all_entry_info()` which performs three unbounded sequential scans over all tx-pool entries with no pagination, entry-count cap, or per-method rate limit. When the pool is large, repeated calls cause sustained CPU and memory pressure and stall the tx-pool service's message loop, blocking transaction submission and block-processing callbacks for the duration of each scan.

## Finding Description
`get_all_entry_info()` in `tx-pool/src/pool.rs` (L464–487) performs three uncapped scans: all `Pending`+`Gap` entries sorted by score, all `Proposed` entries sorted by score, and all entries in `conflicts_cache`. The result is fully materialized in memory before being returned. [1](#0-0) 

This is called directly from the RPC handler in `rpc/src/module/pool.rs` (L703–718) with no guard, no pagination parameter, and no per-method rate limit. [2](#0-1) 

The tx-pool is bounded by byte size (`max_tx_pool_size = 180_000_000`), not entry count. [3](#0-2) 

The only server-side guard is a batch-request count limit, which does not apply to individual method calls. [4](#0-3) 

No rate limiter exists anywhere in the `rpc/` module for this or any other method. The tx-pool service uses an actor/message-passing model, meaning `get_all_entry_info` processing blocks the service's event loop for the full duration of the scan, serializing it against all other pending messages (e.g., `submit_transaction`, block-commit callbacks).

## Impact Explanation
The concrete impact is **performance degradation of a CKB node**: sustained hammering of this endpoint when the pool is large causes CPU saturation from JSON serialization, memory spikes from materializing large response payloads, and stalling of the tx-pool service message loop, which delays transaction acceptance and block processing. This maps to **Low (501–2000 points): Any other important performance improvements for CKB**. The impact does not reliably reach "easily crash a CKB node" (High) because: (a) filling 180 MB of pool space at 1,000 shannons/KB requires a non-trivial real-cost commitment; (b) the RPC is bound to `127.0.0.1` by default, restricting the attacker to local or co-located processes unless the operator has explicitly exposed it; (c) the observed effect is degradation and latency increase, not a definitive process crash.

## Likelihood Explanation
By default the RPC is local-only (`127.0.0.1:8114`), so the attacker must be a local process or the operator must have exposed the port publicly. Filling the pool to maximize impact requires paying minimum fees across ~180 MB of transactions — a real but finite cost. If the pool is naturally full (e.g., during high network activity), the cost barrier drops to zero and only repeated RPC calls are needed. The attack is repeatable and requires no special privilege beyond RPC access.

## Recommendation
1. Add `offset` and `limit` parameters to `get_raw_tx_pool` and slice the iterator accordingly, preventing full-pool scans per call.
2. Add a per-method rate limit for `get_raw_tx_pool` in the RPC server middleware, analogous to the P2P relay rate limiter already used in `sync/src/relayer/mod.rs`.
3. Cap the verbose response at a configurable maximum entry count and document truncation behavior.

## Proof of Concept
1. Start a CKB node with default config.
2. Fill the tx-pool by submitting minimum-fee transactions via `send_transaction` until `max_tx_pool_size` is approached.
3. From 8+ concurrent threads, loop calling `POST /` with `{"method":"get_raw_tx_pool","params":[true],"jsonrpc":"2.0","id":1}`.
4. Observe: node CPU pegged on JSON serialization, `submit_transaction` latency increases significantly, block-processing callbacks stall waiting for the tx-pool service to process their messages.
5. Measurable indicator: compare `submit_transaction` round-trip latency with and without concurrent `get_raw_tx_pool(verbose=true)` hammering on a pool with tens of thousands of entries.

### Citations

**File:** tx-pool/src/pool.rs (L464-487)
```rust
    pub(crate) fn get_all_entry_info(&self) -> TxPoolEntryInfo {
        let pending = self
            .pool_map
            .score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])
            .map(|entry| (entry.transaction().hash(), entry.to_info()))
            .collect();

        let proposed = self
            .pool_map
            .sorted_proposed_iter()
            .map(|entry| (entry.transaction().hash(), entry.to_info()))
            .collect();

        let conflicted = self
            .conflicts_cache
            .iter()
            .map(|(_id, tx)| tx.hash())
            .collect();
        TxPoolEntryInfo {
            pending,
            proposed,
            conflicted,
        }
    }
```

**File:** rpc/src/module/pool.rs (L703-718)
```rust
    fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool> {
        let tx_pool = self.shared.tx_pool_controller();

        let raw = if verbose.unwrap_or(false) {
            let info = tx_pool
                .get_all_entry_info()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Verbose(info.into())
        } else {
            let ids = tx_pool
                .get_all_ids()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Ids(ids.into())
        };
        Ok(raw)
    }
```

**File:** resource/ckb.toml (L211-211)
```text
max_tx_pool_size = 180_000_000 # 180mb
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
