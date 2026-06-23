### Title
Malicious Peer Can Exhaust `OrphanPool` via Cheap Orphan Transactions, Evicting Legitimate Entries - (File: tx-pool/src/component/orphan.rs)

---

### Summary

The `OrphanPool` in CKB's tx-pool enforces a hardcoded global cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` with no per-peer contribution limit. A malicious relay peer can fill the pool with 100 cheap orphan transactions (syntactically valid transactions referencing non-existent input cells), causing legitimate orphan transactions to be randomly evicted. When a legitimate orphan's parent later arrives, `process_orphan_tx` cannot find the evicted child, breaking the automatic resolution chain.

---

### Finding Description

`tx-pool/src/component/orphan.rs` defines the global orphan pool:

```
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
``` [1](#0-0) 

`add_orphan_tx()` inserts any orphan transaction from any peer with no per-peer quota check, then calls `limit_size()`: [2](#0-1) 

`limit_size()` first removes expired entries, then **randomly** evicts entries until the pool is at or below 100: [3](#0-2) 

The eviction is `self.entries.keys().next()` — the first key of a `HashMap`, which is effectively random due to Rust's hash randomization. There is no tracking of which peer contributed which orphan, and no per-peer cap.

The entry point is the relay protocol. When a relayed transaction's inputs are missing from the chain or pool, `after_process` in `tx-pool/src/process.rs` calls `add_orphan`: [4](#0-3) 

Input resolution fails **before** script execution, so the attacker's transactions do not need valid lock-script signatures — they only need to be syntactically valid molecule-encoded `Transaction` structures referencing non-existent `OutPoint`s. The relay handler in `sync/src/relayer/transaction_hashes_process.rs` accepts up to `MAX_RELAY_TXS_NUM_PER_BATCH = 32767` hashes per message: [5](#0-4) 

**Attack steps:**

1. Attacker connects to a victim CKB node as a relay peer.
2. Attacker crafts 100 syntactically valid transactions whose inputs reference non-existent `OutPoint`s (no valid signatures required; input resolution fails before script execution).
3. Attacker relays these transactions; each is rejected with a "missing input" error and added to the orphan pool — filling it to capacity (100 entries).
4. A legitimate user's orphan transaction arrives. It is inserted (pool = 101), then `limit_size()` randomly evicts one entry.
5. Attacker continuously re-relays their 100 orphan transactions to maintain pool saturation.
6. The legitimate orphan transaction is repeatedly evicted and never present when its parent arrives.

---

### Impact Explanation

When `process_orphan_tx` is called after a parent transaction is accepted, it calls `find_by_previous` to locate children in the orphan pool: [6](#0-5) 

If the legitimate child orphan was evicted, it is not found, and the automatic resolution chain is broken. The legitimate user's transaction is silently dropped from the orphan pool; the user must re-submit manually. A sustained attack prevents any legitimate orphan transaction from ever being resolved automatically, disrupting transaction propagation for all peers connected to the victim node.

---

### Likelihood Explanation

**High.** The attack requires:
- A single P2P connection to the victim node (any unprivileged peer).
- 100 syntactically valid `Transaction` structs with arbitrary non-existent `OutPoint` inputs — no CKB ownership, no valid signatures, no PoW.
- Continuous re-relay of the same 100 transactions to maintain saturation (trivially automated).

The pool limit of 100 is very small, making saturation trivial.

---

### Recommendation

Add a per-peer contribution counter to `OrphanPool`. Track how many orphan entries each `PeerIndex` has contributed and reject new orphan insertions from peers that have exceeded a per-peer quota (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_peers`). This mirrors the fix pattern in the original report: the check should be on the **contributor's** count, not the global pool size.

```diff
// In add_orphan_tx, before inserting:
+ let peer_count = self.entries.values().filter(|e| e.peer == peer).count();
+ if peer_count >= MAX_ORPHAN_PER_PEER {
+     return vec![];
+ }
```

---

### Proof of Concept

```
1. Attacker connects to victim CKB node via relay protocol (SupportProtocols::RelayV3).

2. Attacker constructs 100 TransactionView objects:
   - Each has one CellInput referencing a random non-existent OutPoint
     (e.g., OutPoint { tx_hash: random_hash, index: 0 })
   - Each has one CellOutput with any lock script
   - No witness / signature required

3. Attacker sends each via RelayTransaction message with declared_cycle = 1.

4. Victim node: each tx fails input resolution → is_missing_input() == true
   → add_orphan(tx, peer, 1) → OrphanPool now has 100 entries.

5. Legitimate user relays a real orphan transaction (e.g., child of an
   in-flight parent). It is inserted → pool = 101 → limit_size() randomly
   evicts one entry (possibly the legitimate one).

6. Attacker re-sends any evicted malicious orphan immediately.
   Pool stays saturated at 100 attacker entries.

7. Legitimate orphan is never present when its parent is committed.
   process_orphan_tx() finds nothing → legitimate tx is lost.
``` [7](#0-6) [8](#0-7)

### Citations

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
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

**File:** tx-pool/src/component/orphan.rs (L134-159)
```rust
    pub fn add_orphan_tx(
        &mut self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) -> Vec<Byte32> {
        if self.entries.contains_key(&tx.proposal_short_id()) {
            return vec![];
        }

        debug!("add_orphan_tx {}", tx.hash());
        self.entries.insert(
            tx.proposal_short_id(),
            Entry::new(tx.clone(), peer, declared_cycle),
        );

        for out_point in tx.input_pts_iter() {
            self.by_out_point
                .entry(out_point)
                .or_default()
                .insert(tx.proposal_short_id());
        }

        // DoS prevention: do not allow OrphanPool to grow unbounded
        self.limit_size()
    }
```

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
```

**File:** tx-pool/src/process.rs (L591-596)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L29-35)
```rust
            if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    relay_transaction_hashes.tx_hashes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
```
