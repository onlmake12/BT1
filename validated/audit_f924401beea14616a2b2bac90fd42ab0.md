### Title
Unbounded Iteration Over All Tx-Pool Entries in `get_raw_tx_pool` RPC Enables Resource Exhaustion — (`rpc/src/module/pool.rs`, `tx-pool/src/pool.rs`)

---

### Summary

The `get_raw_tx_pool` RPC handler calls `get_all_entry_info()` or `get_ids()`, which iterate over every entry in the tx-pool without any pagination or result-size cap. Any unprivileged RPC caller can first fill the tx-pool with many minimum-size transactions (each paying the minimum fee rate), then repeatedly invoke `get_raw_tx_pool(verbose=true)` to trigger O(N) iteration, O(N) HashMap allocation, and O(N) JSON serialization across the entire pool on every call. This causes sustained CPU and memory exhaustion on the node.

---

### Finding Description

**Entry point — RPC handler:**

In `rpc/src/module/pool.rs` lines 703–718, `get_raw_tx_pool` dispatches to either `get_all_entry_info()` (verbose) or `get_all_ids()` (non-verbose) with no pagination parameter and no result-size limit:

```rust
fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool> {
    let tx_pool = self.shared.tx_pool_controller();
    let raw = if verbose.unwrap_or(false) {
        let info = tx_pool
            .get_all_entry_info()   // <-- iterates entire pool
            ...
        RawTxPool::Verbose(info.into())
    } else {
        let ids = tx_pool
            .get_all_ids()          // <-- iterates entire pool
            ...
        RawTxPool::Ids(ids.into())
    };
    Ok(raw)
}
```

**Unbounded iteration — pool layer:**

`get_all_entry_info()` in `tx-pool/src/pool.rs` lines 464–487 iterates over every pending, gap, proposed, and conflicted entry with no limit:

```rust
pub(crate) fn get_all_entry_info(&self) -> TxPoolEntryInfo {
    let pending = self
        .pool_map
        .score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])
        .map(|entry| (entry.transaction().hash(), entry.to_info()))
        .collect();   // collects ALL entries into a HashMap
    let proposed = self
        .pool_map
        .sorted_proposed_iter()
        .map(|entry| (entry.transaction().hash(), entry.to_info()))
        .collect();   // collects ALL entries into a HashMap
    ...
}
```

**Secondary path — `estimate_fee_rate` RPC:**

`estimate_fee_rate` in `tx-pool/src/process.rs` line 950 also calls `get_all_entry_info()` and then passes the entire result to `WeightUnitsFlow::estimate_fee_rate()` (`util/fee-estimator/src/estimator/weight_units_flow.rs` lines 173–184), which collects all pending+proposed entries into a `Vec` and sorts them O(N log N):

```rust
let all_entry_info = self.tx_pool.read().await.get_all_entry_info();
// ...
let mut current_txs: Vec<_> = all_entry_info
    .pending.into_values()
    .chain(all_entry_info.proposed.into_values())
    .map(TxStatus::new_from_entry_info)
    .collect();
current_txs.sort_unstable_by(|a, b| b.cmp(a));
```

**Attacker-controlled pool growth:**

The tx-pool is bounded by `max_tx_pool_size` bytes (default ~180 MB). With minimum-size CKB transactions (~100–200 bytes each), the pool can hold on the order of hundreds of thousands to over a million entries. An unprivileged user submits transactions via `send_transaction` RPC (or P2P relay) paying only the minimum fee rate. Once the pool is filled, the attacker repeatedly calls `get_raw_tx_pool(verbose=true)` or `estimate_fee_rate` at no additional cost.

---

### Impact Explanation

Each `get_raw_tx_pool(verbose=true)` call:
1. Acquires a read lock on the tx-pool (blocking writers)
2. Allocates a new `HashMap` containing all N entries' full detail structs
3. Serializes the entire HashMap to JSON — potentially hundreds of megabytes per response

Each `estimate_fee_rate` call additionally performs an O(N log N) sort over all entries.

With no rate limiting and no pagination on either RPC, an attacker can issue these calls in a tight loop. The result is:
- Sustained heap allocation pressure (multiple concurrent calls each allocate O(N) memory)
- CPU saturation from serialization and sorting
- Potential OOM or severe latency degradation for all other RPC callers and internal pool operations
- The read lock held during iteration delays any concurrent pool writes (new tx admission, reorg processing)

This constitutes a **service availability attack** against the node's RPC layer and tx-pool, reachable by any unprivileged RPC caller.

---

### Likelihood Explanation

- **Attacker cost**: Paying minimum fee rate to fill the pool. Once filled, the pool does not drain unless blocks commit the transactions. The attacker can maintain pool saturation at low ongoing cost.
- **No privilege required**: `get_raw_tx_pool` and `estimate_fee_rate` are standard public RPC methods.
- **No rate limiting**: CKB's RPC layer applies no per-method rate limiting or response-size cap.
- **Amplification**: One pool-fill operation enables unlimited free exploitation calls.

Likelihood is **medium** — it requires upfront fee payment to fill the pool, but the subsequent exploitation loop is free and repeatable.

---

### Recommendation

1. **Add pagination to `get_raw_tx_pool`**: Accept `after_cursor` and `limit` parameters; return at most a fixed number of entries per call (e.g., 1000).
2. **Avoid full-pool snapshot in `estimate_fee_rate`**: Instead of calling `get_all_entry_info()` and sorting the entire pool on every RPC call, maintain an incrementally updated sorted structure in the fee estimator that is updated on tx admission/removal callbacks.
3. **Apply per-method RPC rate limiting**: Limit the call frequency of expensive pool-scan RPCs per client IP.
4. **Cap response size**: Enforce a maximum serialized response size and return an error or truncated result if exceeded.

---

### Proof of Concept

1. Connect to a CKB node with RPC exposed.
2. Submit a large number of minimum-fee-rate transactions via `send_transaction` until `tx_pool_info` shows the pool near `max_tx_pool_size`.
3. In a loop, call:
   ```json
   {"id":1,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[true]}
   ```
4. Observe: each call triggers full pool iteration and returns a response proportional to pool size; concurrent calls multiply memory allocation; node RPC latency for all other methods degrades.
5. Alternatively, call `estimate_fee_rate` in a loop to trigger the O(N log N) sort on every call.

**Expected outcome**: Node CPU and memory usage spike proportionally to pool fill level; other RPC calls experience increased latency or timeouts; with a sufficiently large pool and high call rate, the node may become unresponsive. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/process.rs (L945-970)
```rust
    pub(crate) async fn estimate_fee_rate(
        &self,
        estimate_mode: EstimateMode,
        enable_fallback: bool,
    ) -> Result<FeeRate, AnyError> {
        let all_entry_info = self.tx_pool.read().await.get_all_entry_info();
        match self
            .fee_estimator
            .estimate_fee_rate(estimate_mode, all_entry_info)
        {
            Ok(fee_rate) => Ok(fee_rate),
            Err(err) => {
                if enable_fallback {
                    let target_blocks =
                        FeeEstimator::target_blocks_for_estimate_mode(estimate_mode);
                    self.tx_pool
                        .read()
                        .await
                        .estimate_fee_rate(target_blocks)
                        .map_err(Into::into)
                } else {
                    Err(err.into())
                }
            }
        }
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L164-185)
```rust
    pub fn estimate_fee_rate(
        &self,
        target_blocks: BlockNumber,
        all_entry_info: TxPoolEntryInfo,
    ) -> Result<FeeRate, Error> {
        if !self.is_ready {
            return Err(Error::NotReady);
        }

        let sorted_current_txs = {
            let mut current_txs: Vec<_> = all_entry_info
                .pending
                .into_values()
                .chain(all_entry_info.proposed.into_values())
                .map(TxStatus::new_from_entry_info)
                .collect();
            current_txs.sort_unstable_by(|a, b| b.cmp(a));
            current_txs
        };

        self.do_estimate(target_blocks, &sorted_current_txs)
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
