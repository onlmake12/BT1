Audit Report

## Title
No-Fee Orphan Pool Flooding via Missing-Input Admission Without Fee Gate — (`tx-pool/src/component/orphan.rs`, `tx-pool/src/process.rs`)

## Summary
The orphan pool admission path in `after_process` stores any transaction whose inputs are unresolvable without performing a fee-rate check. `limit_size` evicts entries using `HashMap::keys().next()`, which is effectively random and not fee-ordered. A single unprivileged remote peer can permanently saturate all 100 orphan slots with zero-fee transactions at the cost of only network bandwidth, causing legitimate orphans to be randomly evicted.

## Finding Description

**Admission path has no fee gate.**

In `process.rs`, `pre_check` calls `resolve_tx`. If resolution fails with any error other than `OutPointError::Dead`, the error propagates directly to the `Err(err) => Err(err)` branch at line 311, bypassing `check_tx_fee` entirely. [1](#0-0) 

Back in `after_process`, the only gate before `add_orphan` is `is_missing_input`. No fee check is performed: [2](#0-1) 

`check_tx_fee` — which enforces `min_fee_rate` — is only reached after successful input resolution and is never called for orphan-bound transactions. [3](#0-2) 

**`add_orphan_tx` stores unconditionally, then calls `limit_size`.** [4](#0-3) 

**`limit_size` evicts randomly, not by fee.** `entries.keys().next()` on a `HashMap` is effectively random — there is no fee-ordered eviction policy. [5](#0-4) 

**Orphan entries persist until `ORPHAN_TX_EXPIRE_TIME` = `100 * MAX_BLOCK_INTERVAL`.** [6](#0-5) 

**No per-peer orphan quota exists.** `add_orphan_tx` performs no per-`PeerIndex` accounting; a single peer can occupy all 100 slots. [7](#0-6) 

## Impact Explanation

Matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

- The orphan pool (100 slots) can be permanently saturated by one attacker peer at zero CKB cost (only network bandwidth required).
- Every legitimate high-fee orphan arriving at a saturated pool faces random eviction; evicted orphans must be re-fetched and re-relayed, increasing relay latency and bandwidth waste across the network.
- The attack is self-sustaining: the attacker continuously replenishes evicted slots, maintaining saturation indefinitely.

## Likelihood Explanation

- Requires only the ability to send P2P relay messages — no CKB tokens, no privileged access, no PoW.
- Constructing 100 structurally valid transactions with fabricated input `OutPoint`s is trivial; they pass `non_contextual_verify` (version, size, non-empty, no dup deps) without issue.
- A single malicious peer is sufficient; no Sybil attack is needed.
- The attack is repeatable and self-sustaining indefinitely.

## Recommendation

1. **Add a fee-rate pre-screen at orphan admission.** Before calling `add_orphan`, perform a lightweight capacity sanity check on declared outputs vs. a minimum threshold, or require a minimum `declared_cycle`-based fee estimate.
2. **Replace random eviction with fee-rate-ordered eviction.** `limit_size` should evict the entry with the lowest `fee / size` ratio (computable from declared cycles and tx size), not a random entry.
3. **Add a per-peer orphan quota.** Limit the number of orphan slots any single `PeerIndex` can occupy (e.g., 10 out of 100), preventing a single peer from saturating the pool.
4. **Reduce `DEFAULT_MAX_ORPHAN_TRANSACTIONS` or add a declared-fee floor.** Require that `declared_cycle` implies a minimum fee rate before admission.

## Proof of Concept

```
1. Attacker constructs 100 transactions T_1..T_100:
   - Each has one input referencing a random, non-existent OutPoint
   - Each has one output with capacity >= input's claimed capacity (zero or negative fee)
   - Each passes NonContextualTransactionVerifier (valid version, size, non-empty, no dup deps)

2. Attacker relays T_1..T_100 to a target node via SendTransaction P2P messages.

3. For each T_i:
   - process_tx → non_contextual_verify (passes)
   - _process_tx → pre_check → resolve_tx → Err(OutPointError::Unknown)
   - after_process → is_missing_input → add_orphan(T_i, peer, declared_cycle)
   - add_orphan_tx inserts T_i; limit_size does nothing (pool not yet full)

4. After T_100: orphan pool is at capacity (100/100).

5. A legitimate node relays a high-fee orphan L:
   - add_orphan_tx inserts L (pool becomes 101)
   - limit_size evicts one entry at random — L has ~1% chance of immediate eviction

6. Attacker monitors evictions and sends T_101, T_102, ... to replace evicted slots,
   maintaining saturation indefinitely.

7. Result: orphan pool remains full of attacker-controlled zero-fee orphans;
   legitimate orphans are continuously at risk of random eviction and must be re-relayed.
```

### Citations

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
