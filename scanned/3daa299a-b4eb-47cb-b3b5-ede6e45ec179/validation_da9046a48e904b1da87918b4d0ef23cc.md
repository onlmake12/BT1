### Title
Attacker Can Grief Legitimate Orphan Transactions via Random Eviction from Bounded OrphanPool — (`tx-pool/src/component/orphan.rs`)

### Summary
The `OrphanPool` in CKB's tx-pool is bounded at `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` entries. When the pool is full, `limit_size()` evicts a **random** entry with no fee-based priority and no per-peer submission limit. A malicious peer can fill all 100 slots with cheap orphan transactions, causing legitimate orphan transactions to be randomly evicted and never processed when their parent arrives.

### Finding Description

The `OrphanPool` struct in `tx-pool/src/component/orphan.rs` enforces a hard cap of 100 entries: [1](#0-0) 

When `add_orphan_tx` is called and the pool is full, `limit_size()` is invoked: [2](#0-1) 

The eviction strategy is `self.entries.keys().next()` — the first key in a `HashMap`, which is effectively arbitrary. There is no fee-rate ordering, no per-peer accounting, and no protection for recently-added legitimate entries. [3](#0-2) 

The `add_orphan_tx` function accepts any peer's transaction with no per-peer quota. The `Entry` struct records the submitting peer but `limit_size()` never consults it during eviction. [4](#0-3) 

**Attacker-controlled entry path:**

1. Attacker connects as a normal relay peer.
2. Attacker sends a `RelayTransactionHashes` message containing up to `MAX_RELAY_TXS_NUM_PER_BATCH = 32767` fake transaction hashes. [5](#0-4) 

3. The victim node adds these to `unknown_tx_hashes` and issues `GetRelayTransactions` requests back to the attacker. [6](#0-5) 

4. The attacker responds with 100 orphan transactions (transactions whose inputs reference non-existent or unconfirmed parents). Because the parent cells are unresolvable, fee verification is skipped and the transactions land directly in the `OrphanPool`.
5. The pool is now full. When a legitimate user's orphan transaction arrives (e.g., a child of a just-broadcast parent), it is inserted as entry 101, and `limit_size()` randomly evicts one entry — with 100/101 probability it evicts a legitimate transaction.
6. The attacker continuously re-fills the pool, keeping the legitimate transaction permanently evicted.

The expire time for orphan entries is `100 * MAX_BLOCK_INTERVAL`, meaning the attacker's entries persist for a very long time without needing to be refreshed: [7](#0-6) 

### Impact Explanation

A legitimate user submitting a child transaction (whose parent is in-flight or just broadcast) will find their orphan transaction repeatedly evicted from the pool. When the parent transaction is eventually confirmed or relayed, the child is no longer in the orphan pool and will not be automatically promoted. The user must resubmit, but the attacker can immediately re-fill the pool, creating a sustained denial of service against orphan transaction processing on the targeted node. This matches the griefing impact class: no profit for the attacker, but persistent damage to legitimate users' transaction flow.

### Likelihood Explanation

Any connected relay peer can execute this attack. No privileged access, no keys, and no significant funds are required. The attacker only needs to maintain a single peer connection and respond to `GetRelayTransactions` with crafted orphan transactions. The pool size of 100 is trivially exhausted. The attack is repeatable indefinitely because evicted entries are simply dropped with no record preventing re-insertion.

### Recommendation

1. **Per-peer eviction priority**: Track how many orphan entries each peer has contributed. When eviction is needed, evict from the peer with the most entries first (similar to Bitcoin Core's orphan pool eviction strategy).
2. **Fee-rate ordering**: Evict the lowest-fee-rate orphan rather than a random one, so cheap attacker transactions are preferentially removed.
3. **Per-peer submission cap**: Enforce a hard limit on how many orphan transactions a single peer may contribute to the pool at any time, analogous to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` used in the relay hash tracking logic. [8](#0-7) 

### Proof of Concept

```
Attacker (malicious relay peer)          Victim CKB Node
─────────────────────────────────────────────────────────
1. Connect via RelayV3 protocol

2. Send RelayTransactionHashes{          
   hashes: [H1, H2, ..., H100]}  ──────► add_ask_for_txs(peer, [H1..H100])
                                          unknown_tx_hashes now has 100 entries

3.                               ◄─────  GetRelayTransactions{tx_hashes: [H1..H100]}

4. Send RelayTransactions{
   txs: [orphan_tx_1, ...,              for each tx: submit_remote_tx()
         orphan_tx_100]}         ──────► parent missing → add_orphan_tx()
                                          OrphanPool.len() == 100

5. Legitimate user submits
   child_tx (parent in-flight)   ──────► add_orphan_tx(child_tx)
                                          len == 101 → limit_size()
                                          random eviction: child_tx evicted
                                          with probability 1/101 per cycle,
                                          but attacker immediately refills →
                                          child_tx never survives in pool

6. Parent tx confirmed on chain           child_tx not in OrphanPool →
                                          child_tx NOT auto-promoted →
                                          user must resubmit manually
                                          (attacker immediately refills again)
```

The root cause is in `tx-pool/src/component/orphan.rs` at the `limit_size` function (lines 96–132) and `add_orphan_tx` (lines 134–159), which together implement a random-eviction, per-peer-unaware bounded pool that any connected peer can exhaust with zero-cost orphan transactions.

### Citations

**File:** tx-pool/src/component/orphan.rs (L15-16)
```rust
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L18-38)
```rust
#[derive(Debug, Clone)]
pub struct Entry {
    /// Transaction
    pub tx: TransactionView,
    /// peer id
    pub peer: PeerIndex,
    /// Declared cycles
    pub cycle: Cycle,
    /// Expire timestamp
    pub expires_at: u64,
}

impl Entry {
    pub fn new(tx: TransactionView, peer: PeerIndex, cycle: Cycle) -> Entry {
        Entry {
            tx,
            peer,
            cycle,
            expires_at: ckb_systemtime::unix_time().as_secs() + ORPHAN_TX_EXPIRE_TIME,
        }
    }
```

**File:** tx-pool/src/component/orphan.rs (L119-125)
```rust
        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }
```

**File:** tx-pool/src/component/orphan.rs (L134-158)
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
```

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L38-50)
```rust
        let tx_hashes: Vec<_> = {
            let mut tx_filter = state.tx_filter();
            tx_filter.remove_expired();
            self.message
                .tx_hashes()
                .iter()
                .map(|x| x.to_entity())
                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                .collect()
        };

        state.add_ask_for_txs(self.peer, tx_hashes)
    }
```

**File:** sync/src/types/mod.rs (L1483-1531)
```rust
    pub fn add_ask_for_txs(&self, peer_index: PeerIndex, tx_hashes: Vec<Byte32>) -> Status {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();

        for tx_hash in tx_hashes
            .into_iter()
            .take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)
        {
            match unknown_tx_hashes.entry(tx_hash) {
                keyed_priority_queue::Entry::Occupied(entry) => {
                    let mut priority = entry.get_priority().clone();
                    priority.push_peer(peer_index);
                    entry.set_priority(priority);
                }
                keyed_priority_queue::Entry::Vacant(entry) => {
                    entry.set_priority(UnknownTxHashPriority {
                        request_time: Instant::now(),
                        peers: vec![peer_index],
                        requested: false,
                    })
                }
            }
        }

        // Check `unknown_tx_hashes`'s length after inserting the arrival `tx_hashes`
        if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
            || unknown_tx_hashes.len()
                >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
        {
            warn!(
                "unknown_tx_hashes is too long, len: {}",
                unknown_tx_hashes.len()
            );

            let mut peer_unknown_counter = 0;
            for (_hash, priority) in unknown_tx_hashes.iter() {
                for peer in priority.peers.iter() {
                    if *peer == peer_index {
                        peer_unknown_counter += 1;
                    }
                }
            }
            if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
                return StatusCode::TooManyUnknownTransactions.into();
            }

            return Status::ignored();
        }

        Status::ok()
```
