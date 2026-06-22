### Title
Unbounded Iteration Over All Pool Entries in `get_raw_tx_pool` RPC Enables Node Resource Exhaustion — (File: `rpc/src/module/pool.rs`)

### Summary
The `get_raw_tx_pool` RPC endpoint, when called with `verbose=true`, invokes `get_all_entry_info()`, which performs three full, uncapped scans of the transaction pool — pending, proposed, and conflicted entries — collecting all results into memory and serializing them to JSON with no pagination or result-size limit. With the default pool capacity of 180 MB and minimum transaction sizes of ~100 bytes, the pool can hold hundreds of thousands of entries. An RPC caller can repeatedly invoke this endpoint to force unbounded iteration, large memory allocation, and prolonged read-lock holding on the tx-pool, exhausting node CPU and RAM.

### Finding Description
In `rpc/src/module/pool.rs`, the `get_raw_tx_pool` handler dispatches to either `get_all_entry_info()` (verbose=true) or `get_all_ids()` (verbose=false), with no limit on the number of entries returned: [1](#0-0) 

The `GetAllEntryInfo` message is handled by acquiring a read lock on the pool and calling `get_all_entry_info()`: [2](#0-1) 

`get_all_entry_info()` in `tx-pool/src/pool.rs` performs three full, unbounded scans: [3](#0-2) 

1. `score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])` — iterates every pending and gap entry.
2. `sorted_proposed_iter()` — iterates every proposed entry.
3. `conflicts_cache.iter()` — iterates every conflicted entry.

All results are `.collect()`-ed into `HashMap`s and serialized to JSON. There is no pagination, no maximum result count, and no response-size cap. The read lock on the pool is held for the entire duration of the call, blocking concurrent write operations (new transaction submissions, block processing updates).

The default pool size is 180 MB: [4](#0-3) 

The `TxPoolConfig` struct confirms `max_tx_pool_size` is the only bound: [5](#0-4) 

### Impact Explanation
With a 180 MB pool and minimum transaction sizes of ~100–200 bytes, the pool can hold on the order of hundreds of thousands to over a million entries. Each `get_raw_tx_pool?verbose=true` call forces the node to:

1. Perform O(n) CPU work iterating and sorting all entries.
2. Allocate O(n) memory to build the full `TxPoolEntryInfo` response (hashes, cycles, fees, ancestor metadata for every entry).
3. Hold the tx-pool read lock for the entire duration, serializing against write-path operations.
4. Emit an unbounded JSON response over the RPC connection.

Repeated rapid calls can exhaust node CPU and RAM, making the RPC service unresponsive and degrading or halting block assembly (`get_block_template`) and transaction relay.

### Likelihood Explanation
The attack requires two steps:

1. **Fill the pool**: A tx-pool submitter (reachable via P2P relay or the `send_transaction` RPC) submits many small transactions paying the minimum fee rate (1,000 shannons/KB). This is cheap and requires no privileged access.
2. **Trigger the scan**: An RPC caller invokes `get_raw_tx_pool` with `verbose=true` repeatedly. By default the RPC listens on `127.0.0.1:8114`, so this requires local access; however, the scope explicitly includes "RPC caller" and "tx-pool submitter" as valid attacker roles. Operators who expose the RPC to a broader network (a common deployment pattern for monitoring) widen the attack surface further.

No rate limiting, authentication, or pagination exists on this endpoint.

### Recommendation
- Add `offset` and `limit` pagination parameters to `get_raw_tx_pool` so callers cannot retrieve the entire pool in a single call.
- Alternatively, impose a hard cap on the maximum number of entries returned per call (e.g., 10,000) and document the limit.
- Consider adding per-IP or per-connection rate limiting to expensive RPC methods.
- Document that operators should restrict RPC access to trusted clients when the pool is large.

### Proof of Concept
1. Configure a node with `min_fee_rate = 1000` and `max_tx_pool_size = 180_000_000` (defaults).
2. Submit a large number of minimal-size transactions via `send_transaction` or P2P relay until the pool approaches its size limit.
3. Repeatedly call:
   ```json
   {"id":1,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[true]}
   ```
4. Observe: each call triggers a full scan of all pool entries in `get_all_entry_info()` at `tx-pool/src/pool.rs:464–487`, holds the pool read lock for the duration, allocates memory proportional to pool size, and returns a response that can be hundreds of megabytes. Sustained repetition exhausts node CPU and RAM and degrades or blocks concurrent pool write operations.

### Citations

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

**File:** tx-pool/src/service.rs (L1000-1006)
```rust
        Message::GetAllEntryInfo(Request { responder, .. }) => {
            let tx_pool = service.tx_pool.read().await;
            let info = tx_pool.get_all_entry_info();
            if let Err(e) = responder.send(info) {
                error!("Responder sending get_all_entry_info failed {:?}", e)
            };
        }
```

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

**File:** resource/ckb.toml (L211-211)
```text
max_tx_pool_size = 180_000_000 # 180mb
```

**File:** util/app-config/src/configs/tx_pool.rs (L11-13)
```rust
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
```
