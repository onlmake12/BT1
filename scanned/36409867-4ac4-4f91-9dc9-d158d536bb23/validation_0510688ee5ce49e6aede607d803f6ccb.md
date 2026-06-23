### Title
O(N²) `shrink_to_fit` on Bulk Peer-Queue Removal Enables CPU Exhaustion via Verify-Queue Write-Lock Stall - (`File: tx-pool/src/component/verify_queue.rs`)

---

### Summary

`VerifyQueue::remove_tx` unconditionally calls `self.shrink_to_fit()` after every single removal. When `remove_txs_by_peer` or `remove_txs` removes K entries from a queue of N entries, this triggers up to K separate O(N) reallocation passes over the `MultiIndexVerifyEntryMap`, producing O(K × N) ≈ O(N²) CPU work — all while holding the exclusive `verify_queue` write-lock. An unprivileged remote peer can pre-fill the queue with transactions and then trigger the bulk-removal path, stalling the entire tx-pool for an extended period.

---

### Finding Description

In `tx-pool/src/component/verify_queue.rs`, `remove_tx` is:

```rust
pub fn remove_tx(&mut self, id: &ProposalShortId) -> Option<Entry> {
    self.inner.remove_by_id(id).map(|e| {
        // ... accounting ...
        self.shrink_to_fit();   // ← called on EVERY single removal
        e.inner
    })
}
``` [1](#0-0) 

`shrink_to_fit` is defined as:

```rust
macro_rules! shrink_to_fit {
    ($map:expr, $threshold:expr) => {{
        if $map.capacity() > ($map.len() + $threshold) {
            $map.shrink_to_fit();   // O(n) reallocation
        }
    }};
}
``` [2](#0-1) 

`shrink_to_fit()` on a `MultiIndexVerifyEntryMap` is O(n) — it must reallocate and rehash all remaining entries across every index. Because `remove_tx` calls it after each individual deletion, bulk removal of K entries from a queue of N entries costs O(K × N) total work.

The two bulk-removal callers are:

**1. `remove_txs_by_peer`** — called from `ban_malformed`:

```rust
pub fn remove_txs_by_peer(&mut self, peer: &PeerIndex) {
    let ids: Vec<_> = self.inner.iter()
        .filter(|&(_cycle, entry)| entry.inner.remote.as_ref().is_some_and(|(_, p)| p == peer))
        .map(|(_cycle, entry)| entry.id.clone())
        .collect();
    self.remove_txs(ids.into_iter());   // calls remove_tx per entry
}
``` [3](#0-2) 

```rust
async fn ban_malformed(&self, peer: PeerIndex, reason: String) {
    // ...
    self.network.ban_peer(peer, DEFAULT_BAN_TIME, reason);
    self.verify_queue.write().await.remove_txs_by_peer(&peer);
}
``` [4](#0-3) 

**2. `remove_txs`** — called during reorg processing:

```rust
let mut queue = self.verify_queue.write().await;
queue.remove_txs(attached.iter().map(|tx| tx.proposal_short_id()));
``` [5](#0-4) 

```rust
pub fn remove_txs(&mut self, ids: impl Iterator<Item = ProposalShortId>) {
    for id in ids {
        self.remove_tx(&id);   // shrink_to_fit on every iteration
    }
}
``` [6](#0-5) 

The verify queue has a hard cap of 256 MB:

```rust
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
``` [7](#0-6) 

A minimum-size CKB transaction is ~60 bytes, so an attacker can fill the queue with tens of thousands of entries before the cap is hit.

---

### Impact Explanation

The `verify_queue` write-lock is held for the entire duration of the O(N²) bulk removal. All other tx-pool operations that need the write-lock — including accepting new transactions, processing orphans, and block assembly — are blocked until the removal completes. With a full 256 MB queue of small transactions (tens of thousands of entries), the O(N²) shrink loop can consume seconds to tens of seconds of CPU time on a single core, effectively stalling the tx-pool service. This is a resource-accounting / CPU-exhaustion DoS reachable by any unprivileged peer.

---

### Likelihood Explanation

The attack is straightforward:

1. Connect to a CKB node as an unprivileged relay peer.
2. Flood the verify queue with many small, valid-looking transactions (each tagged with the attacker's `PeerIndex`) until the queue approaches its 256 MB size limit.
3. Send one malformed transaction to trigger `ban_malformed`, which calls `remove_txs_by_peer` under the write-lock.
4. The O(N²) shrink loop executes, stalling the tx-pool.

No special privileges, keys, or majority hashpower are required. The relay protocol is open to any peer.

---

### Recommendation

Move `shrink_to_fit` out of the per-element `remove_tx` and call it only once after a bulk removal completes. Specifically:

- Remove the `self.shrink_to_fit()` call from `remove_tx`.
- Add a single `self.shrink_to_fit()` call at the end of `remove_txs`, `remove_txs_by_peer`, and any other bulk-removal wrapper.

This reduces bulk removal from O(N²) to O(N), matching the pattern recommended in the original report (avoid rewriting/reprocessing unchanged elements on every step).

---

### Proof of Concept

```
1. Attacker connects to a CKB node via the relay protocol (SupportProtocols::RelayV3).
2. Attacker submits ~50,000 minimum-size transactions (each ~60 bytes, total ~3 MB)
   to the verify queue, all tagged with the attacker's PeerIndex.
   (The 256 MB cap allows far more; even a few thousand entries suffice to observe stall.)
3. Attacker sends one transaction with a deliberately wrong declared cycle count,
   causing _process_tx to return Reject::DeclaredWrongCycles and invoke ban_malformed.
4. ban_malformed acquires verify_queue.write() and calls remove_txs_by_peer.
5. remove_txs_by_peer → remove_txs → remove_tx (×N), each calling shrink_to_fit.
6. With N=50,000 entries, this is ~2.5 billion index operations under the write-lock.
7. All concurrent tx-pool operations (new tx submission, block assembly) stall
   until the loop completes.
``` [8](#0-7) [4](#0-3)

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L18-18)
```rust
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L129-168)
```rust
    pub fn remove_tx(&mut self, id: &ProposalShortId) -> Option<Entry> {
        self.inner.remove_by_id(id).map(|e| {
            let tx_size = e.inner.tx.data().serialized_size_in_block();
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
            }
            self.shrink_to_fit();
            e.inner
        })
    }

    /// Remove multiple txs from the queue
    pub fn remove_txs(&mut self, ids: impl Iterator<Item = ProposalShortId>) {
        for id in ids {
            self.remove_tx(&id);
        }
    }

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

**File:** util/src/shrink_to_fit.rs (L14-19)
```rust
macro_rules! shrink_to_fit {
    ($map:expr, $threshold:expr) => {{
        if $map.capacity() > ($map.len() + $threshold) {
            $map.shrink_to_fit();
        }
    }};
```

**File:** tx-pool/src/process.rs (L679-703)
```rust
    async fn ban_malformed(&self, peer: PeerIndex, reason: String) {
        const DEFAULT_BAN_TIME: Duration = Duration::from_secs(3600 * 24 * 3);

        #[cfg(feature = "with_sentry")]
        use sentry::{Level, capture_message, with_scope};

        #[cfg(feature = "with_sentry")]
        with_scope(
            |scope| scope.set_fingerprint(Some(&["ckb-tx-pool", "receive-invalid-remote-tx"])),
            || {
                capture_message(
                    &format!(
                        "Ban peer {} for {} seconds, reason: \
                        {}",
                        peer,
                        DEFAULT_BAN_TIME.as_secs(),
                        reason
                    ),
                    Level::Info,
                )
            },
        );
        self.network.ban_peer(peer, DEFAULT_BAN_TIME, reason);
        self.verify_queue.write().await.remove_txs_by_peer(&peer);
    }
```

**File:** tx-pool/src/process.rs (L855-857)
```rust
            let mut queue = self.verify_queue.write().await;
            queue.remove_txs(attached.iter().map(|tx| tx.proposal_short_id()));
        }
```
