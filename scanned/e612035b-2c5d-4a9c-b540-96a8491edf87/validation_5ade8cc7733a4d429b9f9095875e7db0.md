### Title
Redundant `mark_as_known_tx` Calls Inside Inner Peer Loop Causes O(txs × peers) Mutex Contention in `send_bulk_of_tx_hashes` — (File: `sync/src/relayer/mod.rs`)

---

### Summary

In `send_bulk_of_tx_hashes`, when a locally RPC-submitted transaction is verified (`original_peer == None`), `mark_as_known_tx` is called **inside** the inner loop over `connected_peers`. This causes the function to acquire two shared mutexes and perform redundant state operations N times per transaction (where N = number of connected peers), instead of once. The correct call site is outside the inner loop, as is done for the `Some(peer)` branch.

---

### Finding Description

`send_bulk_of_tx_hashes` in `sync/src/relayer/mod.rs` contains a nested loop:

```
outer: for tx_verify_result in tx_verify_results          // up to MAX_RELAY_TXS_NUM_PER_BATCH
    inner: for target in &connected_peers                  // all full-relay peers
        if original_peer == None:
            hashes.push(tx_hash.clone());
            self.shared.state().mark_as_known_tx(tx_hash.clone());  // ← called N times
``` [1](#0-0) 

`mark_as_known_tx` delegates to `mark_as_known_txs`, which acquires **two** `Mutex` locks (`unknown_tx_hashes` and `tx_filter`) on every invocation:

```rust
pub fn mark_as_known_txs(&self, hashes: ...) {
    let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
    let mut tx_filter = self.tx_filter.lock();
    for hash in hashes {
        unknown_tx_hashes.remove(&hash);
        tx_filter.insert(hash);
    }
}
``` [2](#0-1) 

For a single verified RPC-submitted transaction with N connected peers, this results in:
- N mutex acquisitions on `unknown_tx_hashes` (instead of 1)
- N mutex acquisitions on `tx_filter` (instead of 1)
- N redundant `tx_filter.insert(hash)` calls — each insertion may reset the TTL entry, extending the hash's lifetime in the filter beyond its intended window
- N redundant `unknown_tx_hashes.remove(&hash)` calls

The `Some(peer)` branch (peer-relayed tx) correctly does **not** call `mark_as_known_tx` inside the inner loop at all, confirming the `None` branch placement is erroneous. [3](#0-2) 

---

### Impact Explanation

The `unknown_tx_hashes` and `tx_filter` mutexes are shared state also accessed by:
- `add_ask_for_txs` — called on every incoming `RelayTransactionHashes` P2P message from any peer
- `pop_ask_for_txs` — called on the relayer's periodic timer to dispatch fetch requests
- `already_known_tx` / `tx_filter()` — called during transaction deduplication

Excessive redundant lock acquisitions from the O(txs × peers) loop create contention on these hot-path mutexes, degrading the node's ability to process incoming relay messages and dispatch outbound tx requests. Additionally, repeated `tx_filter.insert` calls for the same hash can extend the TTL of that hash in the filter, causing the node to incorrectly suppress re-relay of a transaction for longer than the protocol intends — a correctness deviation that affects off-chain processes relying on timely relay propagation. [4](#0-3) 

---

### Likelihood Explanation

The vulnerable path is triggered whenever:
1. A transaction is submitted via the local JSON-RPC (`send_transaction`) — a supported, unprivileged local RPC user action
2. The transaction passes verification and produces `TxVerificationResult::Ok { original_peer: None, .. }`
3. `send_bulk_of_tx_hashes` is invoked on its periodic timer tick

Any operator or local RPC caller can submit transactions. The amplification factor equals the number of full-relay connected peers, which grows with node connectivity. A well-connected node with many peers and a moderate RPC submission rate will experience proportionally higher mutex contention. [5](#0-4) 

---

### Recommendation

Move the `mark_as_known_tx` call **outside** the inner `for target in &connected_peers` loop, so it executes exactly once per verified transaction hash. The corrected structure should be:

```rust
TxVerificationResult::Ok { original_peer, tx_hash } => {
    if original_peer.is_none() {
        self.shared.state().mark_as_known_tx(tx_hash.clone()); // ← once per tx
    }
    for target in &connected_peers {
        match original_peer {
            Some(peer) if peer == *target => {}
            _ => {
                let hashes = selected.entry(*target)...;
                hashes.push(tx_hash.clone());
            }
        }
    }
}
```

This matches the pattern used in `block_proposal_process.rs` where `mark_as_known_tx` is called once per transaction, not once per peer. [6](#0-5) 

---

### Proof of Concept

1. Start a CKB node with several full-relay peers connected (e.g., 10–50 peers).
2. Submit a batch of transactions via the `send_transaction` JSON-RPC endpoint (local RPC caller).
3. Each verified transaction produces `TxVerificationResult::Ok { original_peer: None, tx_hash }`.
4. On the next `send_bulk_of_tx_hashes` timer tick, for each such tx, `mark_as_known_tx` is called once per connected peer — e.g., 50 transactions × 50 peers = 2,500 mutex acquisitions instead of 50.
5. Observe increased lock contention on `unknown_tx_hashes` and `tx_filter`, delaying `add_ask_for_txs` processing for incoming peer relay messages and `pop_ask_for_txs` dispatch, degrading transaction relay throughput proportional to peer count. [7](#0-6)

### Citations

**File:** sync/src/relayer/mod.rs (L631-643)
```rust
    pub async fn send_bulk_of_tx_hashes(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
        const BUFFER_SIZE: usize = 42;

        let connected_peers = nc.full_relay_connected_peers();
        if connected_peers.is_empty() {
            return;
        }

        let tx_verify_results = self
            .shared
            .state()
            .take_relay_tx_verify_results(MAX_RELAY_TXS_NUM_PER_BATCH);
        let mut selected: HashMap<PeerIndex, Vec<Byte32>> = HashMap::default();
```

**File:** sync/src/relayer/mod.rs (L645-671)
```rust
            for tx_verify_result in tx_verify_results {
                match tx_verify_result {
                    TxVerificationResult::Ok {
                        original_peer,
                        tx_hash,
                    } => {
                        for target in &connected_peers {
                            match original_peer {
                                Some(peer) => {
                                    // broadcast tx hash to all connected peers except original peer
                                    if peer != *target {
                                        let hashes = selected
                                            .entry(*target)
                                            .or_insert_with(|| Vec::with_capacity(BUFFER_SIZE));
                                        hashes.push(tx_hash.clone());
                                    }
                                }
                                None => {
                                    // since this tx is submitted through local rpc, it is assumed to be a new tx for all connected peers
                                    let hashes = selected
                                        .entry(*target)
                                        .or_insert_with(|| Vec::with_capacity(BUFFER_SIZE));
                                    hashes.push(tx_hash.clone());
                                    self.shared.state().mark_as_known_tx(tx_hash.clone());
                                }
                            }
                        }
```

**File:** sync/src/types/mod.rs (L1432-1451)
```rust
    pub fn mark_as_known_tx(&self, hash: Byte32) {
        self.mark_as_known_txs(iter::once(hash));
    }

    pub fn remove_from_known_txs(&self, hash: &Byte32) {
        self.tx_filter.lock().remove(hash);
    }

    // maybe someday we can use
    // where T: Iterator<Item=Byte32>,
    // for<'a> &'a T: Iterator<Item=&'a Byte32>,
    pub fn mark_as_known_txs(&self, hashes: impl Iterator<Item = Byte32> + std::clone::Clone) {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
        let mut tx_filter = self.tx_filter.lock();

        for hash in hashes {
            unknown_tx_hashes.remove(&hash);
            tx_filter.insert(hash);
        }
    }
```

**File:** sync/src/relayer/block_proposal_process.rs (L57-62)
```rust
        for (previously_in, tx) in removes.into_iter().zip(unknown_txs) {
            if previously_in {
                sync_state.mark_as_known_tx(tx.hash());
                asked_txs.push(tx);
            }
        }
```
