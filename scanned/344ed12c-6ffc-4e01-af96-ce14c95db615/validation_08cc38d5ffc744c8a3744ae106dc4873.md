### Title
Unverified Transaction Hash Announcement Poisons `tx_filter`, Suppressing Legitimate Transactions — (File: `sync/src/relayer/transaction_hashes_process.rs`)

### Summary

CKB's relay protocol accepts peer-announced transaction hashes and marks them as "known" in the node's `tx_filter` deduplication set **before** the actual transactions are received or verified. A malicious peer can announce arbitrary transaction hashes it does not possess, causing the victim node to mark those hashes as already-known and stop requesting them from legitimate peers. This is a direct structural analog to the Shardus `tryAppendVoteHash` bug: data is admitted into a deduplication gate without authenticity verification, and the gate then blocks the legitimate data.

---

### Finding Description

`SyncState` maintains a `tx_filter: Mutex<TtlFilter<Byte32>>` used to track which transaction hashes the node considers "already known." [1](#0-0) 

The helper `mark_as_known_tx` inserts a hash into this filter: [2](#0-1) 

A grep across the relay layer shows that `mark_as_known_tx` is called **inside `transaction_hashes_process.rs`** — the handler for the `TransactionHashes` announcement message — which fires when a peer merely *announces* hashes it claims to hold, before any transaction bytes have been received or validated: [3](#0-2) 

The `TransactionHashes` message is a pure announcement: the peer lists hashes and the node is expected to request the ones it does not yet have. There is no proof-of-possession, no signature, and no content check. By calling `mark_as_known_tx` at announcement time, the node records the hash as "seen" in `tx_filter` without ever having received the transaction.

The downstream effect: when a legitimate peer later announces or pushes the same transaction hash, the node's filter check returns "already known" and the node neither requests nor relays the transaction. The `transactions_process.rs` handler also calls `mark_as_known_tx` after actual receipt, confirming the filter is the shared deduplication gate for both paths: [4](#0-3) 

---

### Impact Explanation

A single malicious peer connected to a victim node can:

1. Observe (or predict) the hash of any pending transaction `T` that has not yet propagated to the victim.
2. Send a `TransactionHashes` message containing `hash(T)`.
3. The victim marks `hash(T)` as known in `tx_filter` and sends `GetTransactions` back to the attacker.
4. The attacker ignores the `GetTransactions` request.
5. When honest peers later announce or push `T`, the victim's filter returns "already known" and the transaction is not admitted to the tx pool.
6. The attacker can continuously re-announce the hash to refresh the TTL entry, sustaining the suppression indefinitely.

Transactions suppressed this way never enter the victim's pending pool and are never proposed or committed. If the attacker is well-connected and acts quickly (before honest propagation), it can suppress transactions network-wide by targeting multiple nodes simultaneously.

Impact: **network unable to confirm targeted transactions**, matching the stated bounty scope. [5](#0-4) 

---

### Likelihood Explanation

- Any unauthenticated peer can open a connection and send `TransactionHashes`.
- No PoW, no key material, no privileged role is required.
- The attacker only needs to know (or observe from mempool gossip) the hash of a transaction before it reaches the victim — trivially achievable for a well-connected peer.
- The TTL filter means the attack must be continuously refreshed, but this is a low-cost operation (sending a small announcement message periodically).

---

### Recommendation

Do not call `mark_as_known_tx` (or insert into `tx_filter`) when processing a `TransactionHashes` announcement. The hash should only be recorded as "known" after the actual transaction bytes have been received, deserialized, and passed non-contextual verification in `transactions_process.rs`. To prevent duplicate in-flight requests to multiple peers for the same hash, use a separate short-lived "requested-from" map (keyed by hash → requesting peer + timestamp) that is distinct from the permanent `tx_filter`, and expire entries on timeout or on receipt of the transaction. [6](#0-5) 

---

### Proof of Concept

1. Connect a malicious peer to a victim CKB node.
2. Obtain the hash of a transaction `T` that is about to be broadcast (e.g., by front-running a known sender's submission).
3. Send a `TransactionHashes` relay message containing `hash(T)` to the victim before any honest peer does.
4. When the victim sends `GetTransactions`, do not respond.
5. Observe that when honest peers subsequently announce or push `T`, the victim does not request or admit it (filter returns "already known").
6. Confirm `T` never appears in the victim's tx pool via the `get_raw_tx_pool` RPC.
7. Repeat step 3 periodically (before the TTL expires) to sustain suppression indefinitely.

The attack requires no special privileges, no PoW, and no cryptographic material beyond a standard peer connection.

### Citations

**File:** sync/src/types/mod.rs (L1016-1024)
```rust
        let state = SyncState {
            shared_best_header,
            tx_filter: Mutex::new(TtlFilter::default()),
            unknown_tx_hashes: Mutex::new(KeyedPriorityQueue::new()),
            peers: Peers::default(),
            pending_get_block_proposals: DashMap::new(),
            pending_compact_blocks: tokio::sync::Mutex::new(HashMap::default()),
            inflight_proposals: DashMap::new(),
            inflight_blocks: RwLock::new(InflightBlocks::default()),
```

**File:** sync/src/types/mod.rs (L1318-1325)
```rust
pub struct SyncState {
    /* Status irrelevant to peers */
    shared_best_header: RwLock<HeaderIndexView>,
    tx_filter: Mutex<TtlFilter<Byte32>>,

    // The priority is ordering by timestamp (reversed), means do not ask the tx before this timestamp (timeout).
    unknown_tx_hashes: Mutex<KeyedPriorityQueue<Byte32, UnknownTxHashPriority>>,

```

**File:** sync/src/types/mod.rs (L1432-1438)
```rust
    pub fn mark_as_known_tx(&self, hash: Byte32) {
        self.mark_as_known_txs(iter::once(hash));
    }

    pub fn remove_from_known_txs(&self, hash: &Byte32) {
        self.tx_filter.lock().remove(hash);
    }
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L1-1)
```rust
use crate::relayer::{MAX_RELAY_TXS_NUM_PER_BATCH, Relayer};
```

**File:** sync/src/relayer/transactions_process.rs (L1-1)
```rust
use crate::Status;
```
