### Title
No Per-Peer Limit in `OrphanPool` Allows a Single Peer to Monopolize the Pool via Random Eviction - (`tx-pool/src/component/orphan.rs`)

### Summary

The `OrphanPool` in `tx-pool/src/component/orphan.rs` enforces a global cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` entries but has **no per-peer limit**. When the pool is full, `limit_size()` evicts entries **randomly** (via `HashMap::keys().next()`). A single unprivileged P2P peer can fill the entire orphan pool with 100 crafted orphan transactions, causing legitimate orphan transactions from honest peers to be continuously and randomly evicted.

### Finding Description

The `OrphanPool` struct stores orphan transactions (transactions whose parent inputs are not yet known) in a `HashMap<ProposalShortId, Entry>`. [1](#0-0) 

The `add_orphan_tx` function inserts a new entry and then calls `limit_size()`: [2](#0-1) 

`limit_size()` first evicts expired entries, then evicts **randomly** until the pool is at or below the cap: [3](#0-2) 

There is **no per-peer count check** anywhere in `add_orphan_tx` or `limit_size()`. The `Entry` struct records the originating `peer: PeerIndex` but this field is never used to enforce a per-peer quota. [4](#0-3) 

The entry path from the network is:

1. A P2P peer sends a `RelayTransactions` message via the Relay protocol.
2. `TransactionsProcess` calls `process_tx` on the tx-pool service.
3. `after_process` detects `is_missing_input` and calls `add_orphan`: [5](#0-4) 

4. `add_orphan` calls `add_orphan_tx` on the pool with no per-peer guard: [6](#0-5) 

The Relayer's rate limiter is keyed by `(PeerIndex, message_item_id)` at 30 req/s, which is more than sufficient for an attacker to fill 100 slots quickly. [7](#0-6) 

### Impact Explanation

An attacker who connects as a single P2P peer can:

1. Craft 100 distinct orphan transactions (child transactions spending non-existent parent outputs — trivially constructed by referencing random/fabricated `OutPoint`s).
2. Relay them to the victim node, filling the orphan pool to its cap of 100.
3. When any honest peer's legitimate orphan transaction arrives, it is inserted and then `limit_size()` randomly evicts one entry. Because the attacker controls 100/101 entries, the legitimate transaction is evicted with ~99% probability.
4. The attacker continuously re-submits any of their own entries that get evicted (the rate limiter allows 30/s, far exceeding what is needed to maintain saturation).

The result is that legitimate orphan transactions — child transactions waiting for their parents to be relayed — are permanently denied entry into the pool. When the parent transaction eventually arrives and `process_orphan_tx` is called, the child is no longer present, so the dependency chain is broken and the child must be re-relayed from scratch (or is lost entirely if the originating peer has moved on). [8](#0-7) 

### Likelihood Explanation

The attack requires only a single standard P2P connection and the ability to construct transactions with missing inputs, which is trivially achievable by any unprivileged network peer. No special keys, hashpower, or Sybil capability is needed. The cost is negligible: orphan transactions are not broadcast to the wider network and do not require on-chain fees to be paid at submission time. The 30 req/s rate limit per message type is not a meaningful barrier — filling 100 slots takes at most a few seconds.

### Recommendation

Enforce a per-peer cap inside `add_orphan_tx`. Before inserting, count how many entries in `self.entries` already belong to `peer` and reject (or evict the peer's own oldest entry) if the count exceeds a threshold such as `DEFAULT_MAX_ORPHAN_TRANSACTIONS / MAX_PEERS_PER_ORPHAN_SLOT`. Alternatively, change the eviction policy in `limit_size()` to prefer evicting entries from the peer with the most entries, rather than evicting randomly, so that a flooding peer's own entries are displaced first. [9](#0-8) 

### Proof of Concept

```
Attacker (single P2P peer) connects to victim node.

Step 1 – Fill the pool:
  For i in 0..100:
    craft tx_i spending OutPoint { tx_hash: random_hash_i, index: 0 }
    send RelayTransactions([tx_i]) to victim

  → OrphanPool.len() == 100, all entries owned by attacker peer.

Step 2 – Victim receives legitimate orphan from honest peer:
  honest_peer sends child_tx (spending unconfirmed parent)
  → add_orphan_tx(child_tx) inserts it → len == 101
  → limit_size() evicts entries.keys().next() (random)
  → With 100/101 probability, child_tx is evicted.

Step 3 – Attacker maintains saturation:
  For each evicted attacker tx (signalled via TxVerificationResult::Reject):
    re-send that tx to victim (rate limiter: 30/s, well within budget)

  → Pool stays at 100 attacker entries indefinitely.
  → Legitimate orphan transactions are continuously denied.

Step 4 – Parent arrives, child is gone:
  honest_peer sends parent_tx
  → process_orphan_tx(parent_tx) finds no children in orphan pool
  → child_tx is never promoted to pending pool
  → transaction chain is broken for the honest user.
``` [10](#0-9) [1](#0-0)

### Citations

**File:** tx-pool/src/component/orphan.rs (L14-16)
```rust
/// 100 max block interval
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L18-28)
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

**File:** tx-pool/src/process.rs (L557-573)
```rust
    pub(crate) async fn add_orphan(
        &self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) {
        let evicted_txs = self
            .orphan
            .write()
            .await
            .add_orphan_tx(tx, peer, declared_cycle);
        // for any evicted orphan tx, we should send reject to relayer
        // so that we mark it as `unknown` in filter
        for tx_hash in evicted_txs {
            self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
        }
    }
```

**File:** tx-pool/src/process.rs (L591-596)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
```

**File:** sync/src/relayer/mod.rs (L89-98)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
```
