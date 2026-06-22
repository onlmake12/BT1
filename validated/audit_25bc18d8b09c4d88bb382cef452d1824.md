### Title
Unbounded Iteration Over Entire Tx-Pool in `get_raw_tx_pool` RPC Causes Resource Exhaustion — (`rpc/src/module/pool.rs`)

---

### Summary

The `get_raw_tx_pool` RPC handler iterates over every entry in the transaction pool without any pagination or count limit. An unprivileged RPC caller can first flood the pool with many small transactions (up to the byte-size cap) and then repeatedly invoke `get_raw_tx_pool?verbose=true`, forcing the node to hold a pool read-lock, allocate a large in-memory result, and serialize it to JSON on every call. This is a direct analog to the Carapace `lockCapital` / `_accruePremiumAndExpireProtections` unbounded-loop pattern: an attacker-controlled collection size drives unbounded work in a critical code path.

---

### Finding Description

`get_raw_tx_pool` in `rpc/src/module/pool.rs` dispatches to one of two pool-wide scans with no upper bound on the number of entries processed: [1](#0-0) 

When `verbose=true`, it calls `get_all_entry_info()`: [2](#0-1) 

`get_all_entry_info()` calls three separate full-pool iterators — `score_sorted_iter_by_statuses` over pending+gap, `sorted_proposed_iter` over proposed, and a full scan of `conflicts_cache` — collecting and materializing a `TxEntryInfo` struct (fee, size, cycles, ancestor counts, timestamps) for every single entry.

When `verbose=false`, `get_ids()` performs the same two full-pool scans, collecting every hash: [3](#0-2) 

The service message handler acquires a pool read-lock for the entire duration of both operations: [4](#0-3) 

The pool's size bound is expressed in **bytes** (`max_tx_pool_size`), not in transaction count. With the default 180 MB cap and a minimum-size transaction (~100 bytes), the pool can hold on the order of 1–2 million entries. Every `get_raw_tx_pool` call with `verbose=true` must iterate and serialize all of them.

A secondary instance of the same pattern exists in `estimate_fee_rate` in `pool_map.rs`, which iterates over every pool entry sorted by score with no early-exit bound other than filling `target_blocks` blocks — if the pool is large and `target_blocks` is large, the entire pool is scanned: [5](#0-4) 

This is exposed via the `estimate_fee_rate` RPC in `rpc/src/module/experiment.rs`.

---

### Impact Explanation

- **CPU exhaustion**: Each call forces a full sort-and-iterate over all pool entries plus JSON serialization of every `TxEntryInfo`. Repeated calls sustain high CPU load.
- **Memory pressure**: The entire pool's worth of `TxEntryInfo` structs are heap-allocated per call before being serialized. With a full pool this can be hundreds of megabytes of transient allocation per request.
- **Read-lock contention**: The pool read-lock is held for the entire scan. While reads do not block other reads, they do contend with write operations (tx submission, block commit, reorg processing), degrading overall pool throughput.
- **Node degradation**: Sustained calls can make the node unresponsive to legitimate transaction submissions and block assembly, directly harming liveness.

---

### Likelihood Explanation

- The RPC endpoint is enabled by default and many operators expose it on non-loopback interfaces.
- No authentication or rate-limiting is applied to `get_raw_tx_pool` in the codebase.
- Filling the pool requires only valid (low-fee) transactions; an attacker can do this cheaply by submitting many small transactions up to the pool's byte limit.
- The attack is repeatable and requires no special privilege — only network access to the RPC port.

---

### Recommendation

1. **Add a count limit / pagination to `get_raw_tx_pool`**: Accept optional `limit` and `after` cursor parameters (as the indexer RPCs already do) and return at most `N` entries per call.
2. **Cap `get_all_entry_info` / `get_ids` internally**: Enforce a hard maximum on the number of entries returned regardless of caller input.
3. **Rate-limit the RPC**: Apply per-IP or global rate limiting to `get_raw_tx_pool` and `estimate_fee_rate`.
4. **Consider bounding pool entry count** in addition to pool byte size, so that the worst-case iteration length is predictable and small.

---

### Proof of Concept

```
# Step 1: flood the pool with many small transactions up to max_tx_pool_size
for i in $(seq 1 100000); do
    ckb-cli tx send --tx <min_size_tx_$i>
done

# Step 2: repeatedly call get_raw_tx_pool verbose=true
while true; do
    curl -s -X POST http://<node>:8114 \
      -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[true],"id":1}'
done
```

Each iteration of step 2 forces the node to acquire the pool read-lock, iterate over all ~100 000 entries, allocate a `TxEntryInfo` per entry, and serialize the entire result to JSON — with no server-side bound on how many entries are processed or how frequently the call may be made. [6](#0-5) [2](#0-1)

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

**File:** tx-pool/src/pool.rs (L448-462)
```rust
    pub(crate) fn get_ids(&self) -> TxPoolIds {
        let pending = self
            .pool_map
            .score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])
            .map(|entry| entry.transaction().hash())
            .collect();

        let proposed = self
            .pool_map
            .sorted_proposed_iter()
            .map(|entry| entry.transaction().hash())
            .collect();

        TxPoolIds { pending, proposed }
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

**File:** tx-pool/src/service.rs (L380-388)
```rust
    /// Returns information about all transactions in the pool.
    pub fn get_all_entry_info(&self) -> Result<TxPoolEntryInfo, AnyError> {
        send_message!(self, GetAllEntryInfo, ())
    }

    /// Returns the IDs of all transactions in the pool.
    pub fn get_all_ids(&self) -> Result<TxPoolIds, AnyError> {
        send_message!(self, GetAllIds, ())
    }
```

**File:** tx-pool/src/service.rs (L1000-1013)
```rust
        Message::GetAllEntryInfo(Request { responder, .. }) => {
            let tx_pool = service.tx_pool.read().await;
            let info = tx_pool.get_all_entry_info();
            if let Err(e) = responder.send(info) {
                error!("Responder sending get_all_entry_info failed {:?}", e)
            };
        }
        Message::GetAllIds(Request { responder, .. }) => {
            let tx_pool = service.tx_pool.read().await;
            let ids = tx_pool.get_ids();
            if let Err(e) = responder.send(ids) {
                error!("Responder sending get_ids failed {:?}", e)
            };
        }
```

**File:** tx-pool/src/component/pool_map.rs (L334-359)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
    }
```
