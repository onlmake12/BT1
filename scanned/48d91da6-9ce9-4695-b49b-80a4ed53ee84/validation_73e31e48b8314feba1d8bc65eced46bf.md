The code confirms all the technical claims. Let me verify the relay filter behavior as well.

All code references check out. The vulnerability is confirmed. Here is the audit report:

---

Audit Report

## Title
Permissionless Orphan-Pool Slot Exhaustion via Zero-Cost Junk Transactions Enables Continuous Eviction of Legitimate Orphan Transactions — (`tx-pool/src/component/orphan.rs`)

## Summary
The `OrphanPool` enforces a global cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` with no per-peer quota and no fee-rate floor for admission. Any unprivileged P2P peer can flood the pool with 100 structurally valid transactions referencing non-existent inputs at zero cost. When the pool is full, eviction is random (HashMap iteration order). Every evicted orphan is sent as `TxVerificationResult::Reject`, which removes it from the relay known-tx filter, breaking the two-phase relay flow for any transaction whose parent is in-flight.

## Finding Description
**Root cause — `tx-pool/src/component/orphan.rs`**

The pool cap is a single global constant with no per-peer tracking: [1](#0-0) 

`add_orphan_tx` inserts any transaction from any peer unconditionally, then calls `limit_size()`: [2](#0-1) 

`limit_size()` evicts by calling `.keys().next()` on a `HashMap`, which is non-deterministic (effectively random): [3](#0-2) 

Fee-rate checks are bypassed because inputs cannot be resolved for orphan transactions. The `after_process` path routes missing-input failures directly to `add_orphan`: [4](#0-3) 

Every evicted orphan hash is sent as `TxVerificationResult::Reject`, which calls `remove_from_known_txs`, removing the hash from the relay bloom filter: [5](#0-4) [6](#0-5) 

Once evicted, the orphan is no longer in the pool. When its parent arrives, `process_orphan_tx` calls `find_by_previous` to look up children — the evicted child is absent and will not be automatically promoted. The child must be re-relayed, and if the attacker maintains saturation, re-relayed children are evicted again.

**Existing guards are insufficient:**
- The `TooManyUnknownTransactions` ban applies only to `RelayTransactionHashes` (hash-announcement) messages via `add_ask_for_txs`. The attacker sends full `RelayTransactions` messages, which follow a different code path and are not subject to this ban. [7](#0-6) 
- The relayer rate limiter (30 req/s per peer per message type) does not prevent the attack; 100 transactions can be sent in under 4 seconds, well within the rate limit. [8](#0-7) 
- `non_contextual_verify` bans peers for malformed transactions, but transactions with fake `OutPoint`s pass structural validation and are not malformed. [9](#0-8) 

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An attacker targeting multiple nodes simultaneously can continuously displace legitimate orphan transactions across the network, degrading the two-phase transaction relay flow for any transaction whose parent is in-flight. The attack requires no CKB tokens, no PoW, and no privileged access — only the ability to connect as a P2P peer and send 100 molecule-serialized transactions with fake `OutPoint`s.

## Likelihood Explanation
The attack is reachable by any unprivileged peer via `RelayV3`. The attacker crafts 100 minimal valid CKB transactions (correct molecule serialization, fake `OutPoint`s). Each passes `non_contextual_verify`, fails at input resolution with `OutPointError::Unknown`, and is admitted to the orphan pool. The attack is cheap, repeatable, and requires no coordination. The attacker re-sends evicted entries to maintain saturation indefinitely. Orphan entries expire after `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL` (~80 minutes), but continuous re-flooding prevents natural expiry from providing relief. [10](#0-9) 

## Recommendation
1. **Per-peer orphan quota**: Track the count of orphan entries per `PeerIndex`. Cap any single peer at `DEFAULT_MAX_ORPHAN_TRANSACTIONS / 4` entries. When the global pool is full, evict the entry belonging to the peer with the most entries first.
2. **Priority-based eviction**: Replace random eviction with eviction of the entry with the lowest declared cycle count (a proxy for fee-rate) or the oldest entry, making zero-cost junk the first to be displaced.
3. **Minimum declared cycle floor**: Reject orphan admission if `declared_cycle` is below a configurable minimum, imposing a small cost on each junk entry.

## Proof of Concept
1. Connect to a CKB node as a `RelayV3` peer.
2. Construct 100 `RelayTransaction` messages, each containing a valid CKB transaction (correct molecule encoding, version 0, one input referencing `OutPoint::new(random_tx_hash, 0)`, one output with valid capacity). Set `cycles` to any non-zero value.
3. Send all 100 messages. Each passes `non_contextual_verify`, fails `pre_check` with `OutPointError::Unknown`, and is admitted via `add_orphan`. After 100 messages, `OrphanPool::len() == 100`.
4. Send a legitimate orphan transaction from an honest peer. `limit_size()` randomly evicts one existing entry. The evicted hash is sent as `TxVerificationResult::Reject` and removed from the relay filter.
5. Re-send any evicted junk entries to maintain saturation. Repeat indefinitely.
6. Verify: when the parent of the legitimate orphan arrives, `process_orphan_tx` finds no child in the pool and does not promote it. The child must be re-relayed externally.

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

**File:** tx-pool/src/process.rs (L322-332)
```rust
    ) -> Result<(), Reject> {
        if let Err(reject) = non_contextual_verify(&self.consensus, tx) {
            if reject.is_malformed_tx()
                && let Some(remote) = remote
            {
                self.ban_malformed(remote.1, format!("reject {reject}"))
                    .await;
            }
            return Err(reject);
        }
        Ok(())
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

**File:** sync/src/relayer/mod.rs (L91-92)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** sync/src/relayer/mod.rs (L673-675)
```rust
                    TxVerificationResult::Reject { tx_hash } => {
                        self.shared.state().remove_from_known_txs(&tx_hash);
                    }
```

**File:** sync/src/types/mod.rs (L1507-1528)
```rust
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
```
