All cited code references are confirmed against the actual repository. Every claim in the report is accurate:

- `OrphanPool` struct has no per-peer tracking field [1](#0-0) 
- `add_orphan_tx` performs no fee or rate validation before inserting [2](#0-1) 
- `limit_size` evicts via `self.entries.keys().next()` with no per-peer weighting [3](#0-2) 
- `is_missing_input` path routes directly to `add_orphan`, bypassing `check_tx_fee` [4](#0-3) 
- `check_tx_fee` is only reached after a successful `resolve_tx` [5](#0-4) 
- Evicted orphans trigger `TxVerificationResult::Reject` back to the relayer [6](#0-5) 

---

Audit Report

## Title
Zero-Cost Orphan Pool Slot Reservation Enables Griefing of Legitimate Orphan Transactions - (File: `tx-pool/src/component/orphan.rs`)

## Summary
Any connected P2P peer can saturate all 100 orphan pool slots by relaying transactions with fabricated input `OutPoint` hashes at zero on-chain cost. Because fee validation is only reached after a successful `resolve_tx`, transactions with missing inputs bypass it entirely and are admitted unconditionally. With no per-peer slot accounting, a single attacker can permanently evict legitimate orphan transactions, causing their relay to fail.

## Finding Description
The normal processing path calls `pre_check` (process.rs L715), which internally calls `resolve_tx`. `check_tx_fee` is only invoked on the success branch of `resolve_tx` (process.rs L289). Transactions with fabricated inputs fail `resolve_tx` with `is_missing_input`, so `check_tx_fee` is never reached. The `after_process` handler detects `is_missing_input` and routes the transaction directly to `add_orphan` (process.rs L507–512), bypassing all fee logic.

`add_orphan_tx` (orphan.rs L134–159) only deduplicates by `proposal_short_id`, inserts the entry, and calls `limit_size`. There is no minimum fee rate check, no declared-cycle plausibility check, and no per-peer accounting. `OrphanPool` holds only `entries: HashMap<ProposalShortId, Entry>` and `by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>` — no field tracks how many slots a given `PeerIndex` occupies (orphan.rs L41–45).

The only DoS guard is the global 100-slot cap (`DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`, orphan.rs L16). When the pool is full, `limit_size` first evicts expired entries, then evicts via `self.entries.keys().next()` (orphan.rs L121) — HashMap iteration order, not attacker-controlled but also not peer-weighted. Each evicted orphan triggers `TxVerificationResult::Reject` back to the relayer (process.rs L570–572), which marks the transaction as unknown and stops re-relaying it.

An attacker crafts transactions with random 32-byte input hashes. These pass `non_contextual_verify` (which does not check UTXO existence or fee rate), enter the verify queue, fail `resolve_tx` with `is_missing_input`, and are admitted to the orphan pool. No CKB balance, no valid UTXO, and no fee payment is required.

## Impact Explanation
This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* The orphan pool is the holding area for child transactions that arrive before their parents — a normal occurrence during high-throughput relay. Saturating it with 100 attacker-controlled entries means legitimate orphan transactions are either immediately evicted or never admitted. Eviction causes `TxVerificationResult::Reject` to be sent back to the relayer, which marks the transaction as unknown and stops re-relaying it, permanently disrupting relay for those transactions. The attack can be sustained indefinitely by continuously refreshing expiring slots at the cost of only network bandwidth.

## Likelihood Explanation
Any connected P2P peer can execute this attack. No CKB balance, no valid UTXO, and no fee payment is required. Sending 100 relay messages with fabricated inputs is sufficient to saturate the pool. The attacker can maintain saturation indefinitely by resending as entries expire (`ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL`). The attack is cheap, repeatable, and requires no special privilege.

## Recommendation
1. **Per-peer orphan limit**: Add a `peer_counts: HashMap<PeerIndex, usize>` field to `OrphanPool`. Cap each peer at `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_connected_peers`. This is the standard Bitcoin Core mitigation.
2. **Declared fee-rate pre-filter**: Before calling `add_orphan`, require that `declared_cycle` implies a minimum fee rate relative to transaction serialized size. Transactions declaring zero or implausibly low cycles should be rejected before orphan admission.
3. **Peer-weighted eviction**: In `limit_size`, prefer evicting entries from the peer with the most orphan slots rather than using HashMap-order eviction.

## Proof of Concept
```
1. Connect to a CKB node as a P2P peer using the Relay protocol.
2. Craft 100 transactions, each with:
   - inputs referencing random 32-byte hashes (non-existent parents)
   - outputs with any valid capacity
   - witnesses that pass NonContextualTransactionVerifier
   - any declared cycle count
3. Send each via RelayTransaction.
4. Each transaction passes non_contextual_verify, enters the verify queue,
   fails pre_check with is_missing_input, and is routed to add_orphan.
5. After 100 messages, OrphanPool.len() == DEFAULT_MAX_ORPHAN_TRANSACTIONS (100).
6. Relay a legitimate child transaction whose real parent is also in-flight.
7. Observe: limit_size evicts one entry (attacker immediately refills),
   or the legitimate orphan is evicted if the pool was already full.
8. The legitimate child's relay fails: TxVerificationResult::Reject is sent
   back to the relayer, which marks the tx as unknown and stops re-relaying.
9. Repeat step 3 continuously to maintain saturation as entries expire.
```

### Citations

**File:** tx-pool/src/component/orphan.rs (L41-45)
```rust
#[derive(Default, Debug, Clone)]
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
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

**File:** tx-pool/src/process.rs (L286-290)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
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

**File:** tx-pool/src/process.rs (L563-573)
```rust
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
