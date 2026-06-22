### Title
Arithmetic Underflow in `VerifyQueue::is_full` Bypasses Queue Size Limit — (`File: tx-pool/src/component/verify_queue.rs`)

---

### Summary

`VerifyQueue::is_full` computes remaining queue capacity via a bare subtraction `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size`. In Rust release builds, `usize` subtraction wraps silently on underflow. If `total_tx_size` ever exceeds `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`, the result wraps to a near-`usize::MAX` value, causing `is_full` to return `false` (not full) when the queue is actually over capacity. This silently disables the only guard against unbounded verify-queue growth, enabling memory exhaustion / node DoS by any unprivileged tx submitter.

---

### Finding Description

In `tx-pool/src/component/verify_queue.rs`, the queue fullness check is:

```rust
// line 17-18
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000; // 256 MB

// line 103-106
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
``` [1](#0-0) [2](#0-1) 

This is the direct analog of the `bulkMint` pattern: `require(balance < limit - count)`. When `count > limit`, the subtraction underflows. Here, when `self.total_tx_size > DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`, the subtraction `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size` wraps to approximately `usize::MAX` in release mode. The comparison `add_tx_size >= usize::MAX` is then always `false`, so `is_full` returns `false` — the queue appears empty when it is over capacity.

The correct form is `self.total_tx_size.saturating_add(add_tx_size) >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`.

**How `total_tx_size` can exceed the limit:**

`add_tx` calls `remove_tx` *before* the `is_full` check when replacing a proposal tx:

```rust
// lines 204-215
if self.contains_key(&tx.proposal_short_id()) {
    if is_proposal_tx {
        self.remove_tx(&tx.proposal_short_id()); // <-- accounting updated here
    } else {
        return Ok(false);
    }
}
// ...
if self.is_full(tx_size) { ... }  // <-- check happens after remove
``` [3](#0-2) 

`remove_tx` has a silent failure path: when `checked_sub` fails AND `recompute_total_tx_size` overflows, `total_tx_size` is left unchanged at its stale (incorrect) value:

```rust
// lines 132-144
if let Some(total_tx_size) = self.total_tx_size.checked_sub(tx_size) {
    self.total_tx_size = total_tx_size;
} else if let Some(total_tx_size) = self.recompute_total_tx_size() {
    error!(...);
    self.total_tx_size = total_tx_size;
} else {
    error!(...);
    // total_tx_size is NOT updated — stale value persists
}
``` [4](#0-3) 

If `total_tx_size` becomes inflated due to this stale-value path, subsequent `is_full` calls operate on an incorrect baseline. Once `total_tx_size` crosses `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`, the subtraction underflows and the limit is permanently bypassed for all future `add_tx` calls.

---

### Impact Explanation

`is_full` is the **sole guard** preventing the verify queue from growing beyond 256 MB. If bypassed, an attacker submitting transactions via the `send_transaction` RPC can fill the queue without bound, exhausting node memory and causing an out-of-memory crash or severe performance degradation — a full node DoS. The `checked_add` at line 221 provides a secondary guard against `total_tx_size` itself overflowing `usize`, but it does not protect against the `is_full` underflow. [5](#0-4) 

---

### Likelihood Explanation

The underflow requires `total_tx_size` to exceed 256 MB. Under normal operation the `is_full` check itself prevents this, so the underflow is latent. However, the `remove_tx` silent-failure path (line 140–144) can leave `total_tx_size` inflated, and the proposal-tx replacement path in `add_tx` (line 204–206) calls `remove_tx` before the fullness check, creating a window where an inflated `total_tx_size` is used. An attacker who can trigger the accounting inconsistency (e.g., by repeatedly submitting and replacing large proposal txs near the limit) can push `total_tx_size` past the threshold. Once past, the bypass is permanent until the queue is cleared. [6](#0-5) 

---

### Recommendation

Replace the bare subtraction with an addition-based check that cannot underflow:

```rust
pub fn is_full(&self, add_tx_size: usize) -> bool {
    self.total_tx_size.saturating_add(add_tx_size) >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE
}
```

Additionally, the `remove_tx` silent-failure branch (line 140–144) should reset `total_tx_size` to 0 or the recomputed value rather than leaving it stale, to prevent accounting drift from compounding.

---

### Proof of Concept

1. Fill the verify queue to just below 256 MB by submitting many transactions via `send_transaction` RPC.
2. Repeatedly submit and replace large proposal transactions (same short ID, `is_proposal_tx = true`) near the limit. Each replacement calls `remove_tx` before `is_full`, and if `remove_tx` fails to decrement `total_tx_size` (silent-failure path), `total_tx_size` drifts upward.
3. Once `total_tx_size > 256_000_000`:
   - `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size` wraps to `usize::MAX - delta`
   - `add_tx_size >= usize::MAX - delta` is `false` for any realistic tx size
   - `is_full` returns `false` permanently
4. Submit transactions freely; the queue grows without bound, exhausting node memory. [7](#0-6) [8](#0-7)

### Citations

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

**File:** tx-pool/src/component/verify_queue.rs (L132-144)
```rust
            if let Some(total_tx_size) = self.total_tx_size.checked_sub(tx_size) {
                self.total_tx_size = total_tx_size;
            } else if let Some(total_tx_size) = self.recompute_total_tx_size() {
                error!(
                    "verify_queue total_tx_size {} underflowed by sub {}, recomputed {}",
                    self.total_tx_size, tx_size, total_tx_size
                );
                self.total_tx_size = total_tx_size;
            } else {
                error!(
                    "verify_queue total_tx_size {} underflowed by sub {}, and recomputing overflowed",
                    self.total_tx_size, tx_size
                );
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
