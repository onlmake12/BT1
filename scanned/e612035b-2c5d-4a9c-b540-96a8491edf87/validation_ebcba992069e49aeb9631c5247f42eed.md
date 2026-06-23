### Title
Verify Queue Bypasses Configured `max_tx_pool_size` Limit, Enabling Unbounded Memory Consumption Beyond Operator Intent — (`tx-pool/src/component/verify_queue.rs`)

---

### Summary

The CKB tx-pool uses a two-stage admission pipeline: transactions first enter a `VerifyQueue` staging area, then are moved into the main `pool_map` after full script verification. The main pool enforces `max_tx_pool_size` via `limit_size()`. However, the `VerifyQueue` enforces a **separate, hardcoded** 256 MB ceiling (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`) that is entirely independent of the operator-configured `max_tx_pool_size`. The `limit_size()` eviction logic never touches the verify queue, and the verify queue's size is not counted in `pool_map.total_tx_size`. An unprivileged attacker (RPC caller or P2P relayer) can flood the verify queue up to 256 MB regardless of what `max_tx_pool_size` is set to, causing the node to consume far more memory than the operator intended.

---

### Finding Description

**Hardcoded verify-queue ceiling independent of `max_tx_pool_size`:**

`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE` is a compile-time constant of 256 MB: [1](#0-0) 

The `is_full` guard in `VerifyQueue::add_tx` checks only against this hardcoded constant, never against the operator-configured `max_tx_pool_size`: [2](#0-1) 

The `add_tx` method enforces this check and rejects only when the queue exceeds 256 MB: [3](#0-2) 

**`limit_size()` never drains the verify queue:**

The main pool's eviction loop only iterates over `pool_map` entries with statuses `Pending`, `Gap`, and `Proposed`. It has no awareness of the verify queue: [4](#0-3) 

**`total_tx_size` excludes verify-queue entries:**

The `TxPoolInfo` struct reports `total_tx_size` from `pool_map` only. The verify queue's size is reported separately as `verify_queue_size` (a count, not bytes) and is never included in the size accounting that drives eviction: [5](#0-4) 

**Consequence — total memory budget is `max_tx_pool_size + 256 MB + orphan_pool`:**

At any moment the node can hold:
- Up to `max_tx_pool_size` bytes in `pool_map` (operator-controlled, default 180 MB)
- Up to 256 MB in `verify_queue` (hardcoded, operator-invisible)
- Up to `DEFAULT_MAX_ORPHAN_TRANSACTIONS × TRANSACTION_SIZE_LIMIT` ≈ 51 MB in the orphan pool [6](#0-5) 

If an operator sets `max_tx_pool_size = 10 MB` to run on a memory-constrained node, the verify queue alone can still consume 256 MB — 25× the intended budget.

---

### Impact Explanation

An attacker who can submit transactions (via `send_transaction` RPC or P2P relay) can fill the verify queue with up to 256 MB of syntactically valid but computationally expensive transactions. Because script verification is CPU-bound and asynchronous, these transactions linger in the queue. The node's actual memory consumption can reach `max_tx_pool_size + 256 MB`, far exceeding the operator's configured limit. On memory-constrained nodes this causes OOM-kill or severe swap pressure, halting block production and P2P connectivity. Even on well-provisioned nodes, the verify queue acts as a free 256 MB staging buffer that any peer can fill, degrading verification throughput for legitimate transactions.

---

### Likelihood Explanation

The entry path requires no privilege: any RPC caller or P2P peer can submit transactions. Transactions only need to pass `non_contextual_verify` (size ≤ 512 KB, not cellbase) before entering the verify queue. An attacker can craft up to ~512 transactions of maximum size (512 KB each) to saturate the 256 MB queue. This is a low-cost, low-skill attack requiring no keys, no hashpower, and no Sybil capability. [7](#0-6) 

---

### Recommendation

1. Replace the hardcoded `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE` with a value derived from `TxPoolConfig::max_tx_pool_size` (e.g., `max_tx_pool_size` or `max_tx_pool_size * 1.5`), so the verify queue respects the operator's memory budget.
2. Include `verify_queue.total_tx_size` in the combined size check so that `limit_size()` can account for in-flight transactions when deciding whether to accept new ones.
3. Alternatively, enforce a per-peer rate limit on verify-queue submissions to bound the rate at which any single source can fill the staging area.

---

### Proof of Concept

1. Configure a CKB node with `max_tx_pool_size = 10_000_000` (10 MB) in `ckb.toml`.
2. Generate ~512 syntactically valid transactions each close to 512 KB (e.g., large witness data, valid lock scripts that are slow to verify).
3. Submit all 512 transactions via `send_transaction` RPC in rapid succession.
4. Observe via `tx_pool_info` that `verify_queue_size` grows to ~512 while `total_tx_size` remains near 0 (transactions are still being verified).
5. The node's RSS memory grows by ~256 MB — 25× the configured `max_tx_pool_size` — while the operator-visible `total_tx_size` metric shows no alarm signal, because the verify queue's byte consumption is invisible to the eviction logic. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L17-19)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
const SHRINK_THRESHOLD: usize = 100;
```

**File:** tx-pool/src/component/verify_queue.rs (L104-106)
```rust
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L198-237)
```rust
    pub fn add_tx(
        &mut self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        if self.contains_key(&tx.proposal_short_id()) {
            if is_proposal_tx {
                self.remove_tx(&tx.proposal_short_id());
            } else {
                return Ok(false);
            }
        }
        let tx_size = tx.data().serialized_size_in_block();
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "verify_queue total_tx_size overflowed, failed to add tx: {:#x}",
                tx.hash()
            ))
        })?;
        self.inner.insert(VerifyEntry {
            id: tx.proposal_short_id(),
            added_time: unix_time_as_millis(),
            inner: Entry { tx, remote },
            is_large_cycle,
            is_proposal_tx,
        });
        self.total_tx_size = total_tx_size;
        self.ready_rx.notify_one();
        Ok(true)
    }
```

**File:** tx-pool/src/pool.rs (L292-329)
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
    }
```

**File:** tx-pool/src/service.rs (L1086-1097)
```rust
            pending_size: tx_pool.pool_map.pending_size(),
            proposed_size: tx_pool.pool_map.proposed_size(),
            orphan_size: orphan.len(),
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
            min_fee_rate: self.tx_pool_config.min_fee_rate,
            min_rbf_rate: self.tx_pool_config.min_rbf_rate,
            last_txs_updated_at: tx_pool.pool_map.get_max_update_time(),
            tx_size_limit: TRANSACTION_SIZE_LIMIT,
            max_tx_pool_size: self.tx_pool_config.max_tx_pool_size as u64,
            verify_queue_size: verify_queue.len(),
        }
```

**File:** tx-pool/src/component/orphan.rs (L15-16)
```rust
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/util.rs (L56-73)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

    // The ckb consensus does not limit the size of a single transaction,
    // but if the size of the transaction is close to the limit of the block,
    // it may cause the transaction to fail to be packed
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
```
