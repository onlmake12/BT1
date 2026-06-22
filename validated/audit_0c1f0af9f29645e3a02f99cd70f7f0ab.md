### Title
Shared `VerifyQueue` Has No Per-Sender Quota, Allowing a Single Actor to Exhaust the 256 MB Global Limit and Block All Legitimate Transaction Submissions — (`tx-pool/src/component/verify_queue.rs`)

---

### Summary

The CKB tx-pool uses a pre-verification staging queue (`VerifyQueue`) with a single global size limit of 256 MB (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`). There is no per-peer or per-RPC-caller quota. A single attacker — either a local RPC caller or a connected P2P peer — can submit many structurally valid but contextually invalid transactions (each up to 512 KB, the per-transaction size limit) to fill the entire queue. Once full, every subsequent legitimate transaction submission is rejected with `Reject::Full`, regardless of origin, until the attacker's transactions are drained by workers.

---

### Finding Description

The `VerifyQueue` is the mandatory staging area that every incoming transaction must pass through before contextual verification. Its admission check is:

```rust
// tx-pool/src/component/verify_queue.rs
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000; // 256 MB

pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
``` [1](#0-0) [2](#0-1) 

When `is_full` returns `true`, `add_tx` immediately returns `Reject::Full`:

```rust
if self.is_full(tx_size) {
    return Err(Reject::Full(format!(
        "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
        tx.hash()
    )));
}
``` [3](#0-2) 

Every transaction submission path — local RPC (`submit_local_tx`), remote P2P relay (`submit_remote_tx`), and `notify_tx` — converges on `resumeble_process_tx`, which calls `enqueue_verify_queue`:

```rust
pub(crate) async fn resumeble_process_tx(...) -> Result<bool, Reject> {
    self.non_contextual_verify(&tx, remote).await?;
    // ...
    self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
}
``` [4](#0-3) 

The only pre-queue check is `non_contextual_verify`, which validates structure and enforces the per-transaction size ceiling of 512 KB:

```rust
let tx_size = tx.data().serialized_size_in_block() as u64;
if tx_size > TRANSACTION_SIZE_LIMIT {  // TRANSACTION_SIZE_LIMIT = 512 * 1_000
    return Err(Reject::ExceededTransactionSizeLimit(...));
}
``` [5](#0-4) [6](#0-5) 

Critically, `non_contextual_verify` does **not** check whether the referenced input cells exist on-chain. That check happens during contextual verification, which occurs asynchronously inside the queue. An attacker can therefore craft transactions with fabricated (non-existent) input `OutPoint`s that pass `non_contextual_verify` but will fail contextual resolution. These transactions occupy queue space until a worker processes and evicts them.

There is no per-peer or per-caller quota anywhere in the queue. The only per-peer mechanism is `remove_txs_by_peer`, which is only invoked on peer disconnect — not as an admission control: [7](#0-6) 

---

### Impact Explanation

**Tx-pool admission DoS.** Once the attacker fills the 256 MB `VerifyQueue`, every new transaction from every source — local RPC users, honest P2P peers, the node's own `notify_tx` path — is rejected with `Reject::Full`. The node's mempool effectively stops accepting new transactions. The attacker can sustain this by continuously re-submitting as workers drain the queue, since each re-submission costs only the CPU of passing `non_contextual_verify` (a cheap structural check). This prevents legitimate transactions from entering the pool, stalling the node's participation in the network's transaction relay and block assembly.

---

### Likelihood Explanation

**High for local RPC attacker; Medium for P2P attacker.**

- **Local RPC path**: The RPC is bound to `127.0.0.1:8114` by default. Any process on the same machine (a malicious co-located process, a compromised application using the node's RPC) can call `send_transaction` in a tight loop. Filling 256 MB requires approximately `256 MB / 512 KB = 512` transactions. Each transaction is structurally trivial to construct (one fake input, one output). This is achievable in seconds. [8](#0-7) 

- **P2P path**: A connected peer can relay transactions via the relay protocol. The `submit_remote_tx` path feeds directly into `resumeble_process_tx_and_notify_full_reject`, which calls `enqueue_verify_queue`. A single malicious peer can exhaust the queue before being disconnected, and reconnect to repeat. [9](#0-8) 

---

### Recommendation

1. **Introduce a per-peer quota** in `VerifyQueue`. Track how much of `total_tx_size` each `PeerIndex` contributes and cap it (e.g., at `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE / max_peers`). The data structure for per-peer removal already exists (`remove_txs_by_peer`); extend it to enforce admission limits.

2. **Introduce a per-peer eviction policy** when the queue is full: instead of a flat `Reject::Full`, evict the lowest-priority transaction from the peer that holds the largest share of the queue, similar to how `limit_size` evicts by fee rate in the main pool.

3. **Apply a minimum fee-rate check before queue admission** (not just after contextual verification), so that zero-fee or dust transactions cannot cheaply fill the queue.

---

### Proof of Concept

**Attacker role**: Local RPC caller (supported attacker profile per scope).

**Steps**:

1. Construct ~512 structurally valid transactions, each with a large witness blob approaching 512 KB, referencing a fabricated (non-existent) input `OutPoint`. Each passes `non_contextual_verify` (structure is valid, size ≤ 512 KB, not a cellbase).

2. Submit all 512 transactions via `send_transaction` RPC in rapid succession. Each call reaches `enqueue_verify_queue` and is accepted into the `VerifyQueue`, accumulating ~256 MB of `total_tx_size`.

3. Observe that the 513th `send_transaction` call (from any caller, including legitimate users) returns:
   ```
   PoolIsFull: Transaction is replaced because the pool is full,
   verify_queue total_tx_size exceeded, failed to add tx: 0x...
   ```

4. Workers begin processing the attacker's transactions; each fails at contextual resolution (fake inputs → `OutPointError::Unknown`) and is evicted. The attacker immediately re-submits to refill the queue.

5. Legitimate transactions are continuously rejected for as long as the attacker maintains the flood. [10](#0-9) [4](#0-3)

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L17-19)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
const SHRINK_THRESHOLD: usize = 100;
```

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L158-168)
```rust
    /// Remove multiple txs from the queue from a specified peer
    pub fn remove_txs_by_peer(&mut self, peer: &PeerIndex) {
        let ids: Vec<_> = self
            .inner
            .iter()
            .filter(|&(_cycle, entry)| entry.inner.remote.as_ref().is_some_and(|(_, p)| p == peer))
            .map(|(_cycle, entry)| entry.id.clone())
            .collect();

        self.remove_txs(ids.into_iter());
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L196-237)
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

**File:** tx-pool/src/process.rs (L335-353)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
    }
```

**File:** tx-pool/src/process.rs (L371-379)
```rust
    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
    }
```

**File:** tx-pool/src/util.rs (L67-73)
```rust
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
```

**File:** util/types/src/core/tx_pool.rs (L309-309)
```rust
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

**File:** resource/ckb.toml (L181-187)
```text
# Please strictly limit the access to only trusted machines.
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
# }}

# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```
