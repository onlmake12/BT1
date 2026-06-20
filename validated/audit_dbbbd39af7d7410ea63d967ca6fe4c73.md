### Title
Unbounded `get_raw_tx_pool` RPC Getter Causes Resource Exhaustion When Pool Is Large — (`rpc/src/module/pool.rs`)

---

### Summary

The `get_raw_tx_pool` RPC method returns the **entire** tx-pool contents in a single response with no pagination or result-size cap. Any RPC caller can trigger this. When the pool is near its 180 MB default limit, the resulting JSON serialization can consume hundreds of megabytes of heap and block the RPC worker thread, degrading or denying service to all other RPC callers.

---

### Finding Description

`get_raw_tx_pool` dispatches to one of two unbounded collectors depending on the `verbose` flag:

**Non-verbose path** — `get_ids()` in `tx-pool/src/pool.rs`: [1](#0-0) 

**Verbose path** — `get_all_entry_info()` in `tx-pool/src/pool.rs`: [2](#0-1) 

Both are called unconditionally from the RPC handler with no limit parameter: [3](#0-2) 

The service layer dispatches these under a held async read-lock on the pool: [4](#0-3) 

The RPC trait signature accepts no `limit` or `after` cursor: [5](#0-4) 

There is no `max_response_body_size` configured anywhere in the RPC layer — only `max_request_body_size = 10485760` (10 MiB) is set: [6](#0-5) 

The tx pool is bounded only by `max_tx_pool_size`, which defaults to **180 MB**: [7](#0-6) [8](#0-7) 

The pool enforces this limit in bytes of raw transaction data, not in number of entries: [9](#0-8) 

A minimum-size CKB transaction is roughly 100–200 bytes. At 180 MB pool capacity, the pool can hold on the order of hundreds of thousands of entries. In verbose mode, each entry serializes to a JSON object with 7 fields plus a 64-hex-char key. The resulting JSON response can be several hundred megabytes — far exceeding the 10 MiB request cap that guards the inbound side.

---

### Impact Explanation

An RPC caller who issues `get_raw_tx_pool(verbose=true)` against a full pool forces the node to:

1. Acquire a read-lock on the tx pool (blocking any concurrent write, e.g., new transaction admission).
2. Allocate and populate a `HashMap<Byte32, TxPoolEntry>` with every pending, proposed, and conflicted entry.
3. Serialize the entire map to JSON — potentially hundreds of megabytes.
4. Transmit the response over the socket.

Repeated calls can exhaust heap memory and saturate CPU on the RPC worker threads, making the node's RPC interface unresponsive to miners (`get_block_template`), wallet integrators (`send_transaction`), and monitoring tools. The read-lock held during collection also delays write-side pool operations.

---

### Likelihood Explanation

- The RPC is exposed by default on `127.0.0.1:8114`. Many operators expose it to a wider network (e.g., behind a reverse proxy or with a custom `listen_address`).
- No authentication is required to call `get_raw_tx_pool`.
- Filling the pool to near-capacity is achievable by any transaction sender paying the minimum fee rate (1,000 shannons/KB).
- The attack requires only two steps: flood the pool with minimum-fee transactions, then repeatedly call `get_raw_tx_pool(verbose=true)`.
- Even without a full pool, a moderately loaded pool (tens of thousands of entries) produces a response large enough to cause measurable latency spikes on the RPC thread.

---

### Recommendation

Mirror the pattern already used by the Indexer and Rich-Indexer RPC modules, which enforce a `limit` parameter and return a cursor for pagination: [10](#0-9) [11](#0-10) 

Specifically:

1. Add a mandatory (or defaulted) `limit: Uint32` and optional `after: Option<JsonBytes>` cursor to `get_raw_tx_pool`.
2. Enforce a server-side maximum (e.g., 500 entries per call) analogous to `self.request_limit` in the indexer.
3. Return a `last_cursor` field so callers can page through the full pool.
4. Alternatively, add a hard cap on the JSON response body size in the RPC server configuration.

---

### Proof of Concept

```bash
# Step 1: fill the pool with minimum-fee transactions until near max_tx_pool_size
# (omitted for brevity — standard CKB transaction submission loop)

# Step 2: trigger unbounded dump
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[true],"id":1}'
# Response JSON grows proportionally to pool size.
# With a 180 MB pool of small transactions, the response can exceed 200 MB,
# exhausting heap and blocking the RPC worker thread for seconds per call.
# Repeated calls degrade or deny service to all other RPC consumers.
```

The root cause is in `tx-pool/src/pool.rs` at `get_all_entry_info()` (lines 464–487) and `get_ids()` (lines 448–462), both called unconditionally from `rpc/src/module/pool.rs` `get_raw_tx_pool` (lines 703–718) with no bound on the number of entries returned.

### Citations

**File:** tx-pool/src/pool.rs (L292-328)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
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

**File:** rpc/src/module/pool.rs (L394-395)
```rust
    #[rpc(name = "get_raw_tx_pool")]
    fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool>;
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

**File:** resource/ckb.toml (L186-187)
```text
# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```

**File:** resource/ckb.toml (L211-211)
```text
max_tx_pool_size = 180_000_000 # 180mb
```

**File:** util/app-config/src/configs/tx_pool.rs (L12-13)
```rust
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
```

**File:** rpc/src/module/indexer.rs (L396-403)
```rust
    #[rpc(name = "get_cells")]
    fn get_cells(
        &self,
        search_key: IndexerSearchKey,
        order: IndexerOrder,
        limit: Uint32,
        after: Option<JsonBytes>,
    ) -> Result<IndexerPagination<IndexerCell>>;
```

**File:** util/indexer/src/service.rs (L388-397)
```rust
        let limit = limit.value() as usize;
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```
