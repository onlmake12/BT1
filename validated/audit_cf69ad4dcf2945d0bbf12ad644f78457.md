### Title
Orphan Transaction Parent-Request State Machine Has No Retry After Single-Peer Non-Response — (`sync/src/types/mod.rs`, `tx-pool/src/component/orphan.rs`)

---

### Summary

When a CKB node receives an orphan transaction (one whose parent cell is unknown), it queues a `GetRelayTransactions` request for the parent and stores the orphan in the `OrphanPool`. If the single announcing peer never responds, the `unknown_tx_hashes` entry is **permanently dropped** after one retry window with no fallback to other peers and no eviction of the now-unresolvable orphan. An unprivileged relay peer can exploit this to fill the 100-slot orphan pool with permanently unresolvable entries, causing random eviction of legitimate orphan transactions for up to 80 minutes per attack wave.

---

### Finding Description

**Step 1 — Orphan admission and parent request queuing**

In `tx-pool/src/process.rs`, when a remotely-relayed transaction fails with a missing-input error, it is added to the `OrphanPool` and a `TxVerificationResult::UnknownParents` result is sent to the relayer: [1](#0-0) 

The relayer's `send_bulk_of_tx_hashes` handles `UnknownParents` by calling `add_ask_for_txs(peer, parents)`, inserting the parent hashes into `unknown_tx_hashes` keyed by the announcing peer: [2](#0-1) 

**Step 2 — The incomplete retry in `pop_ask_for_txs`**

`pop_ask_for_txs` is called on every `ASK_FOR_TXS_TOKEN` tick. It pops entries from `unknown_tx_hashes` and calls `next_request_peer()` to decide which peer to ask: [3](#0-2) 

`next_request_peer()` returns `None` when `requested == true` and `peers.len() <= 1` — i.e., the transaction was announced by exactly one peer and that peer has already been asked once: [4](#0-3) 

When `next_request_peer()` returns `None`, the entry is **not re-pushed** to `unknown_tx_hashes`. It is silently discarded. There is no fallback to other peers, no longer-timeout re-queue, and no eviction of the orphan whose parent will now never be fetched.

**Step 3 — Orphan pool stays full**

The orphan pool entry created in Step 1 remains until `ORPHAN_TX_EXPIRE_TIME`: [5](#0-4) 

`ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL = 100 * 48 = 4800 seconds ≈ 80 minutes`.

When the pool exceeds `DEFAULT_MAX_ORPHAN_TRANSACTIONS` (100), random eviction occurs: [6](#0-5) 

---

### Impact Explanation

An attacker who is a connected relay peer can:

1. Send 100 valid transactions whose inputs reference non-existent (attacker-controlled) cells — these are syntactically valid but have unknown parents.
2. The victim node adds all 100 to the orphan pool and sends `GetRelayTransactions` to the attacker.
3. The attacker ignores all requests.
4. After `RETRY_ASK_TX_TIMEOUT_INCREASE` (30 seconds), `pop_ask_for_txs` drops all 100 `unknown_tx_hashes` entries permanently.
5. The orphan pool is now full for up to 80 minutes.
6. Any legitimate orphan transaction arriving during this window is randomly evicted, breaking the orphan-resolution chain for honest users.

The analog to the RocketPool finding is direct: the "challenge" (orphan tx admission + parent request) can be initiated by any peer, but the "response" side (retry to other peers, or orphan eviction on request abandonment) is not implemented. The orphan pool plays the role of the locked tribute — capacity consumed with no recovery path until expiry.

---

### Likelihood Explanation

- Any peer that opens the `RelayV3` protocol can send `RelayTransactions` messages.
- Constructing valid-but-orphan transactions requires only a valid signature over a non-existent input — trivially achievable.
- The attack requires only 100 such transactions to saturate the pool.
- No privileged role, no majority hashpower, no social engineering required.

---

### Recommendation

1. **Re-queue on exhausted peers**: In `pop_ask_for_txs`, when `next_request_peer()` returns `None`, re-insert the entry with a significantly longer timeout (e.g., 5 minutes) rather than silently dropping it, so that a future peer announcement can still resolve it.
2. **Evict orphan on request abandonment**: When the `unknown_tx_hashes` entry for a parent hash is permanently dropped, proactively remove the corresponding orphan transaction from `OrphanPool` rather than letting it occupy a slot until expiry.
3. **Attribute orphan slots per peer**: Limit the number of orphan transactions accepted from a single peer to prevent one peer from monopolizing the 100-slot pool.

---

### Proof of Concept

```
1. Attacker connects to victim node, opens RelayV3 protocol.
2. Attacker generates 100 transactions spending outputs of non-existent cells
   (valid signatures, unknown OutPoints).
3. Attacker sends RelayTransactions for all 100 to the victim.
4. Victim: adds all 100 to OrphanPool; sends GetRelayTransactions to attacker.
5. Attacker: ignores all GetRelayTransactions messages.
6. After RETRY_ASK_TX_TIMEOUT_INCREASE (30s), pop_ask_for_txs fires:
   - next_request_peer() returns None for each entry (single peer, already requested).
   - All 100 unknown_tx_hashes entries are dropped permanently.
7. OrphanPool is now full (100/100) for ~80 minutes.
8. Any legitimate orphan tx submitted during this window is randomly evicted,
   breaking dependent transaction chains for honest users.
``` [7](#0-6) [8](#0-7)

### Citations

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
```

**File:** sync/src/relayer/mod.rs (L676-686)
```rust
                    TxVerificationResult::UnknownParents { peer, parents } => {
                        let tx_hashes: Vec<_> = {
                            let mut tx_filter = self.shared.state().tx_filter();
                            tx_filter.remove_expired();
                            parents
                                .into_iter()
                                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                                .collect()
                        };
                        self.shared.state().add_ask_for_txs(peer, tx_hashes);
                    }
```

**File:** sync/src/types/mod.rs (L1276-1289)
```rust
    pub fn next_request_peer(&mut self) -> Option<PeerIndex> {
        if self.requested {
            if self.peers.len() > 1 {
                self.request_time = Instant::now();
                self.peers.swap_remove(0);
                self.peers.first().cloned()
            } else {
                None
            }
        } else {
            self.requested = true;
            self.peers.first().cloned()
        }
    }
```

**File:** sync/src/types/mod.rs (L1453-1481)
```rust
    pub fn pop_ask_for_txs(&self) -> HashMap<PeerIndex, Vec<Byte32>> {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
        let mut result: HashMap<PeerIndex, Vec<Byte32>> = HashMap::new();
        let now = Instant::now();

        if !unknown_tx_hashes
            .peek()
            .map(|(_tx_hash, priority)| priority.should_request(now))
            .unwrap_or_default()
        {
            return result;
        }

        while let Some((tx_hash, mut priority)) = unknown_tx_hashes.pop() {
            if priority.should_request(now) {
                if let Some(peer_index) = priority.next_request_peer() {
                    result
                        .entry(peer_index)
                        .and_modify(|hashes| hashes.push(tx_hash.clone()))
                        .or_insert_with(|| vec![tx_hash.clone()]);
                    unknown_tx_hashes.push(tx_hash, priority);
                }
            } else {
                unknown_tx_hashes.push(tx_hash, priority);
                break;
            }
        }
        result
    }
```

**File:** tx-pool/src/component/orphan.rs (L14-16)
```rust
/// 100 max block interval
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L96-132)
```rust
    fn limit_size(&mut self) -> Vec<Byte32> {
        let now = ckb_systemtime::unix_time().as_secs();
        let expires: Vec<_> = self
            .entries
            .iter()
            .filter_map(|(id, entry)| {
                if entry.expires_at <= now {
                    Some(id)
                } else {
                    None
                }
            })
            .cloned()
            .collect();

        let mut evicted_txs = vec![];

        for id in expires {
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        if !evicted_txs.is_empty() {
            trace!("OrphanTxPool full, evicted {} tx", evicted_txs.len());
            self.shrink_to_fit();
        }
        evicted_txs
    }
```
