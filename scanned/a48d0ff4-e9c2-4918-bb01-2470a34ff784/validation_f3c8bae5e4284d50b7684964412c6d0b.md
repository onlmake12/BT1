### Title
Incomplete Pool Size Accounting Omits `verify_queue` Size in `TxPool::limit_size` — (`File: tx-pool/src/pool.rs`)

---

### Summary

The `TxPool::limit_size` function enforces the configured `max_tx_pool_size` limit by checking only `pool_map.total_tx_size`. It does not account for the size of transactions currently staged in the `VerifyQueue`. Because the `VerifyQueue` has its own independent hardcoded size cap (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256 MB`), the combined memory consumed by the tx-pool subsystem can reach `max_tx_pool_size + 256 MB`, silently violating the operator-configured resource bound. An unprivileged transaction submitter can exploit this to cause the node to consume far more memory than intended.

---

### Finding Description

The CKB tx-pool is split into two distinct structures:

1. **`PoolMap`** — holds verified, admitted transactions (pending/gap/proposed states). Its size is tracked in `pool_map.total_tx_size`.
2. **`VerifyQueue`** — a staging area for transactions awaiting script verification before admission to `PoolMap`. Its size is tracked in `verify_queue.total_tx_size`.

The pool size enforcement function `TxPool::limit_size` evicts entries only when:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
``` [1](#0-0) 

This check is entirely blind to `verify_queue.total_tx_size`. The `VerifyQueue` enforces its own independent hardcoded ceiling:

```rust
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
...
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
``` [2](#0-1) [3](#0-2) 

The two limits are completely independent. The `limit_size` eviction loop never reads `verify_queue.total_tx_size`, so the "pending" memory already reserved by the verify queue is never subtracted from the available budget when deciding whether to evict pool entries.

The `TxPoolInfo` struct reported to operators also reflects only `pool_map.total_tx_size`, not the combined total:

```rust
total_tx_size: tx_pool.pool_map.total_tx_size,
...
verify_queue_size: verify_queue.len(),   // count only, not bytes
``` [4](#0-3) 

This is structurally identical to the Bluefin pattern: a guard checks `available - locked` but omits `- pending`, allowing the reserved-but-not-yet-committed amount to inflate actual usage beyond the intended ceiling.

---

### Impact Explanation

The effective maximum memory consumed by the tx-pool subsystem is:

```
max_tx_pool_size  (configurable, default 180 MB)
+ DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE  (hardcoded 256 MB)
= up to ~436 MB
```

A node operator who sets `max_tx_pool_size = 10 MB` to conserve memory on a constrained host will find the node can actually consume up to ~266 MB for the tx-pool alone. Under sustained transaction flooding, this can trigger OOM conditions, cause the OS to kill the node process, or degrade performance for all node services — constituting a remote denial-of-service against any reachable CKB full node.

---

### Likelihood Explanation

The entry path is fully unprivileged. Any peer or RPC caller can submit transactions via `send_transaction` (RPC) or the P2P relay path (`submit_remote_tx`), both of which route directly into `VerifyQueue::add_tx`:

```rust
async fn enqueue_verify_queue(...) -> Result<bool, Reject> {
    let mut queue = self.verify_queue.write().await;
    queue.add_tx(tx, is_proposal_tx, remote)
}
``` [5](#0-4) 

An attacker needs only to submit enough distinct transactions to saturate the verify queue (256 MB) while the pool_map is also near its configured limit. No special privilege, key, or majority hashpower is required. The hardcoded 256 MB verify-queue ceiling is not operator-adjustable, so the gap cannot be closed by configuration alone.

---

### Recommendation

The `limit_size` eviction condition should incorporate the current `verify_queue` size so that the combined occupancy is bounded by `max_tx_pool_size`:

```rust
// In TxPool::limit_size, pass verify_queue_size as a parameter:
while self.pool_map.total_tx_size + verify_queue_size > self.config.max_tx_pool_size {
    // evict ...
}
```

Alternatively, `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE` should be derived from `max_tx_pool_size` rather than being a hardcoded constant, and the two limits should be treated as a shared budget. The `TxPoolInfo` response should also expose `verify_queue` byte usage (not just entry count) so operators can observe actual combined memory pressure.

---

### Proof of Concept

1. Attacker connects to a CKB node (or uses the public `send_transaction` RPC endpoint).
2. Attacker submits a stream of valid-looking transactions (e.g., spending unspendable inputs — they only need to pass pre-verification checks to enter the verify queue). Each transaction is ~500 bytes serialized.
3. The verify queue accepts up to `256_000_000 / 500 ≈ 512,000` such transactions before `is_full` returns `true`.
4. Simultaneously, the attacker submits valid transactions that enter `pool_map`. The pool_map enforces `max_tx_pool_size` (e.g., 180 MB) independently.
5. The node's tx-pool subsystem now holds `~256 MB (verify_queue) + ~180 MB (pool_map) = ~436 MB`, while `tx_pool_info.total_tx_size` reports only the pool_map portion and `limit_size` never fires based on the combined total.
6. On a node configured with `max_tx_pool_size = 10 MB`, the actual usage is ~266 MB — 26× the configured limit — with no eviction triggered from the pool_map side. [6](#0-5) [7](#0-6)

### Citations

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

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L196-220)
```rust
    /// If the queue did not have this tx present, true is returned.
    /// If the queue did have this tx present, false is returned.
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
```

**File:** tx-pool/src/service.rs (L1089-1096)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
            min_fee_rate: self.tx_pool_config.min_fee_rate,
            min_rbf_rate: self.tx_pool_config.min_rbf_rate,
            last_txs_updated_at: tx_pool.pool_map.get_max_update_time(),
            tx_size_limit: TRANSACTION_SIZE_LIMIT,
            max_tx_pool_size: self.tx_pool_config.max_tx_pool_size as u64,
            verify_queue_size: verify_queue.len(),
```

**File:** tx-pool/src/process.rs (L860-868)
```rust
    async fn enqueue_verify_queue(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        let mut queue = self.verify_queue.write().await;
        queue.add_tx(tx, is_proposal_tx, remote)
    }
```
