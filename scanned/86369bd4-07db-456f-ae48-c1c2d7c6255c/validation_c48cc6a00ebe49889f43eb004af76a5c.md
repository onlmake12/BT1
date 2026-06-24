Audit Report

## Title
Orphan Transaction Pool Flooding Allows Attacker to Evict Legitimate Orphan Transactions - (File: tx-pool/src/component/orphan.rs)

## Summary
The `OrphanPool` enforces a global hard cap of 100 entries (`DEFAULT_MAX_ORPHAN_TRANSACTIONS`) with no per-peer accounting. Any relay peer can flood the pool with fake orphan transactions referencing non-existent inputs, saturating the pool and causing legitimate in-flight child transactions to be evicted via non-deterministic HashMap-order eviction. Evicted transactions are silently removed from the node's known-tx filter with no re-request mechanism, breaking transaction chains on the attacked node.

## Finding Description

The constant and eviction logic are confirmed in the actual code: [1](#0-0) 

The `limit_size()` function evicts via `self.entries.keys().next()` — HashMap iteration order, providing no fairness or per-peer weighting: [2](#0-1) 

`add_orphan_tx` performs no per-peer accounting before insertion: [3](#0-2) 

The entry path is confirmed: when `_process_tx` returns `is_missing_input`, the transaction is unconditionally added to the orphan pool: [4](#0-3) 

Evicted orphan hashes are sent as `TxVerificationResult::Reject`: [5](#0-4) 

The relayer handles `Reject` by calling `remove_from_known_txs` — removing the tx from the node's filter with no re-request: [6](#0-5) 

`MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767` (equal to `MAX_RELAY_TXS_NUM_PER_BATCH`) provides no protection since it operates on a completely separate data structure from the 100-entry orphan pool: [7](#0-6) 

The `add_ask_for_txs` per-peer check only triggers after the global `unknown_tx_hashes` queue is already saturated, not before orphan pool insertion: [8](#0-7) 

**Attack flow:**
1. Attacker connects as a relay peer and sends `RelayTransactionHashes` with fake tx hashes.
2. Victim issues `GetRelayTransactions`; attacker responds with transactions referencing non-existent `OutPoint`s.
3. Each transaction fails with `is_missing_input` → added to `OrphanPool`. Pool saturates at 100 entries.
4. A legitimate child transaction (parent in-flight) arrives → `limit_size()` evicts a random entry, potentially the legitimate child.
5. Evicted child's hash is sent as `TxVerificationResult::Reject` → `remove_from_known_txs`. Node silently drops it.
6. When the legitimate parent arrives, `process_orphan_tx` finds no children. The child is lost from this node.
7. Attacker repeats every `ORPHAN_TX_EXPIRE_TIME` seconds to maintain saturation.

Note: CKB's `is_missing_input` check occurs before signature verification, meaning the attacker's fake transactions do not require valid signatures — only structurally valid transaction format with non-existent input `OutPoint`s. This lowers the attack cost further.

## Impact Explanation

**High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker operating multiple peer connections can simultaneously target many nodes, disrupting transaction propagation network-wide. Legitimate child transactions are silently evicted and lost from attacked nodes' orphan pools. When parents arrive, chains are broken with no notification to the original sender. At scale, this degrades transaction propagation reliability across the network. The attack is repeatable indefinitely with minimal cost.

## Likelihood Explanation

The attack requires only a standard relay peer connection. No proof-of-work, no privileged keys, no Sybil attack, and no valid signatures are required — only structurally valid transactions with non-existent inputs. The orphan pool cap of 100 is small enough to saturate in a single request-response round trip. The attacker can sustain the attack by re-sending fake orphans as old ones expire (`ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL`). Cost per attack cycle: 100 minimal transactions per targeted node.

## Recommendation

1. **Per-peer orphan pool accounting**: Track orphan entry counts per peer. Reject new entries from peers that have already contributed more than `DEFAULT_MAX_ORPHAN_TRANSACTIONS / expected_peers` entries.
2. **Peer-weighted eviction**: When the pool is full, prefer to evict entries from the peer with the highest contribution count rather than using HashMap iteration order.
3. **Increase or make the cap configurable**: The current cap of 100 is very small for a network with many concurrent in-flight transaction chains.
4. **Re-request evicted orphans**: When a legitimate orphan is evicted, consider notifying the originating peer so it can re-relay rather than silently dropping.

## Proof of Concept

```
1. Attacker connects to victim CKB node as a relay peer.

2. Attacker sends RelayTransactionHashes with 100 fake tx hashes.

3. Victim issues GetRelayTransactions. Attacker responds with 100
   structurally valid CKB transactions, each spending a random
   non-existent OutPoint (no valid signature required — missing input
   check fires before signature verification).

4. Each transaction fails _process_tx with is_missing_input →
   add_orphan_tx called → OrphanPool reaches 100 entries (capacity).

5. Legitimate user's child_tx (parent in-flight) arrives via relay.
   add_orphan_tx → limit_size() → evicts via entries.keys().next().
   child_tx may be the evicted entry.

6. Evicted child_tx hash → TxVerificationResult::Reject →
   remove_from_known_txs. Node silently drops child_tx with no
   re-request and no notification to sender.

7. Legitimate parent_tx arrives → process_orphan_tx finds no children.
   child_tx is permanently lost from this node's orphan pool.

8. Attacker re-sends step 2-3 every ORPHAN_TX_EXPIRE_TIME to maintain
   saturation.

Verification: Add a unit test to OrphanPool that inserts 100 fake
entries from a single peer, then inserts a legitimate entry, and
asserts the legitimate entry may appear in the evicted list returned
by add_orphan_tx.
```

### Citations

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
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

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
```

**File:** tx-pool/src/process.rs (L568-572)
```rust
        // for any evicted orphan tx, we should send reject to relayer
        // so that we mark it as `unknown` in filter
        for tx_hash in evicted_txs {
            self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
        }
```

**File:** sync/src/relayer/mod.rs (L673-674)
```rust
                    TxVerificationResult::Reject { tx_hash } => {
                        self.shared.state().remove_from_known_txs(&tx_hash);
```

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```

**File:** sync/src/types/mod.rs (L1506-1529)
```rust
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
```
