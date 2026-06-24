Audit Report

## Title
Orphan Transaction Pool Exhaustion via Fee-Free Relay — (`File: tx-pool/src/component/orphan.rs`)

## Summary
The `OrphanPool` accepts transactions with unresolvable inputs unconditionally, with no fee-rate gate, up to a hard cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`. When full, it evicts entries pseudo-randomly by iterating `HashMap` keys. Any single P2P peer can saturate the pool with 100 zero-fee crafted transactions at zero on-chain cost, continuously displacing legitimate orphan transactions and degrading child-before-parent transaction propagation on the targeted node.

## Finding Description

**Constants confirmed** in `tx-pool/src/component/orphan.rs`: [1](#0-0) 

**Random eviction** in `limit_size()` uses `HashMap::keys().next()`, which is non-deterministic: [2](#0-1) 

**Unconditional orphan admission** in `after_process()`: when `is_missing_input(reject)` is true, `add_orphan()` is called with no fee-rate check: [3](#0-2) 

**`add_orphan` path** directly calls `add_orphan_tx` and sends `TxVerificationResult::Reject` for any evicted entries, causing relayers to mark them `unknown` in their bloom filters: [4](#0-3) 

**`check_tx_fee` is unreachable for orphans**: in `pre_check()`, `check_tx_fee` is only called after `resolve_tx` succeeds. A transaction with unknown inputs fails resolution and returns `Err(err)` at line 311, which propagates to `after_process` → `add_orphan` before `check_tx_fee` is ever invoked: [5](#0-4) 

**`is_missing_input`** confirms the trigger condition — only `OutPointError::Unknown` qualifies: [6](#0-5) 

The exploit chain is: craft structurally valid transactions with random `OutPoint` inputs → pass `non_contextual_verify` → fail resolution with `OutPointError::Unknown` → `is_missing_input` returns `true` → `add_orphan_tx` inserts unconditionally → `limit_size` randomly evicts legitimate entries → evicted hashes sent as `Reject` to relayers → relayers mark them `unknown` and stop re-requesting.

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. The attack costs zero CKB, requires only a single P2P connection per target node, and is trivially parallelizable across all reachable nodes simultaneously. Applied broadly, it degrades the network's ability to propagate child-before-parent transactions, as legitimate orphans are continuously evicted before their parents arrive. The `Reject` signal sent to relayers causes bloom-filter poisoning, preventing re-request of evicted transactions.

## Likelihood Explanation

The attack requires no funds, no special privileges, and no victim mistakes — only a standard P2P connection. Crafting structurally valid transactions with fake `OutPoint`s is trivial. The pool size of 100 is saturatable in a single burst. The random eviction policy means the attacker does not need to predict which slot to target; continuous insertion at one tx per eviction cycle maintains saturation. `ORPHAN_TX_EXPIRE_TIME` of `100 * MAX_BLOCK_INTERVAL` provides no meaningful relief during an active attack. [1](#0-0) 

## Recommendation

1. **Per-peer orphan quota**: track how many orphan slots each `PeerIndex` occupies in `OrphanPool` and cap per-peer contribution (e.g., 10 of 100 slots), preventing a single peer from monopolizing the pool.
2. **Fee-rate-ordered eviction**: replace the random `HashMap::keys().next()` eviction with eviction of the entry with the lowest declared cycle-per-byte ratio, analogous to how the main pool's `limit_size` uses `EvictKey`.
3. **Minimum absolute fee floor at admission**: even without resolved inputs, reject orphans whose output capacity sum cannot plausibly satisfy `min_fee_rate` once resolved. [2](#0-1) 

## Proof of Concept

```
1. Attacker connects to victim CKB node via RelayV3 P2P protocol.

2. Attacker constructs 100 transactions T_1..T_100 where each T_i:
   - Has one input referencing OutPoint { tx_hash: random_bytes_32, index: 0 }
   - Has one output with capacity = MIN_CELL_CAPACITY (zero fee)
   - Has a valid witness (always-success lock or empty)
   - Passes NonContextualTransactionVerifier (structurally valid)

3. Attacker relays T_1..T_100 via RelayTransaction messages.

4. For each T_i, the victim node:
   a. non_contextual_verify passes
   b. pre_check → resolve_tx → OutPointError::Unknown → Err propagated
   c. after_process: is_missing_input() returns true
   d. add_orphan(T_i, peer, declared_cycle) called
   e. add_orphan_tx inserts T_i; limit_size() randomly evicts if pool > 100

5. OrphanPool is now full with T_1..T_100 (all attacker-controlled).

6. Legitimate user relays child_tx (parent not yet seen by victim):
   a. child_tx enters orphan pool (pool size becomes 101)
   b. limit_size() randomly evicts one entry — 100/101 probability it evicts child_tx
   c. Evicted child_tx hash sent as TxVerificationResult::Reject to relayer
   d. Relayer marks child_tx unknown in bloom filter; not re-requested

7. Attacker continuously sends new fake orphans to maintain saturation.
```

Unit test plan: initialize `OrphanPool`, insert 100 attacker-controlled entries, insert one legitimate entry, call `add_orphan_tx` for a 101st attacker entry, assert the legitimate entry is evicted with high probability over repeated trials. Confirm `send_result_to_relayer` emits `TxVerificationResult::Reject` for the evicted hash. [7](#0-6) [8](#0-7)

### Citations

**File:** tx-pool/src/component/orphan.rs (L15-16)
```rust
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
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

**File:** tx-pool/src/process.rs (L286-312)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
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

**File:** tx-pool/src/process.rs (L557-572)
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
```

**File:** tx-pool/src/util.rs (L150-152)
```rust
pub(crate) fn is_missing_input(reject: &Reject) -> bool {
    matches!(reject, Reject::Resolve(out_point_err) if out_point_err.is_unknown())
}
```
