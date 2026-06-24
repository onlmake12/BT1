The code confirms all the claims. Let me verify the key facts:

**Admission path (no fee gate):** In `process.rs`, `_process_tx` calls `pre_check` which calls `resolve_tx`. If inputs are unknown, `resolve_tx` returns `Err(Reject::Resolve(OutPointError::Unknown))`, which propagates out of `pre_check` and is caught by `try_or_return_with_snapshot!` — returning `Some((Err(reject), snapshot))` before `check_tx_fee` is ever reached. Back in `after_process`, `is_missing_input` is true and `add_orphan` is called unconditionally. [1](#0-0) [2](#0-1) 

**Random eviction confirmed:** `limit_size` uses `self.entries.keys().next()` on a `HashMap`, which has no ordering guarantee. [3](#0-2) 

**No per-peer quota confirmed:** `add_orphan_tx` only checks for duplicate `proposal_short_id`, not per-peer count. [4](#0-3) 

**Constants confirmed:** [5](#0-4) 

---

Audit Report

## Title
No-Fee Orphan Pool Flooding via Missing-Input Admission Without Fee Gate — (`tx-pool/src/component/orphan.rs`, `tx-pool/src/process.rs`)

## Summary
The orphan pool admission path in `after_process` stores any transaction whose inputs are unresolvable without performing any fee-rate check, because `check_tx_fee` is only reachable after successful input resolution in `pre_check`. Combined with random (non-fee-ordered) eviction in `limit_size` and the absence of any per-peer quota, an unprivileged remote peer can permanently saturate all 100 orphan slots with zero-fee transactions at the cost of only network bandwidth, causing legitimate orphans to be randomly evicted.

## Finding Description
**Root cause:** In `_process_tx` (`process.rs:715`), `pre_check` is called. Inside `pre_check` (`process.rs:286-311`), `resolve_tx` is called first; if any input references an unknown UTXO, it returns `Err(Reject::Resolve(OutPointError::Unknown))`, which propagates out of `pre_check`. The macro `try_or_return_with_snapshot!` (`process.rs:717`) immediately returns `Some((Err(reject), snapshot))` — `check_tx_fee` at `util.rs:28` is never reached. Control returns to `process_tx` (`process.rs:417`), which calls `after_process`. In `after_process` (`process.rs:507-512`), `is_missing_input(reject)` is true, so `add_orphan` is called with no fee gate whatsoever.

**Eviction is random:** `limit_size` (`orphan.rs:119-125`) uses `self.entries.keys().next()` on a `HashMap`, which provides no ordering. There is no fee-rate-ordered eviction.

**No per-peer quota:** `add_orphan_tx` (`orphan.rs:134-159`) only deduplicates by `proposal_short_id`. A single peer can fill all 100 slots.

**Persistence:** `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL` (`orphan.rs:15`). Attacker-controlled orphans whose parents are never submitted persist for the full expiry window, and the attacker can continuously send fresh unique orphans to replace any that expire or are evicted.

**Exploit flow:**
1. Attacker constructs 100 transactions each with one input referencing a random non-existent `OutPoint` and outputs with capacity ≥ inputs (fee irrelevant — never checked).
2. Each passes `non_contextual_verify` (valid version, size, non-empty, no dup deps).
3. Each is relayed via `SendTransaction` P2P message → `process_tx` → `non_contextual_verify` passes → `_process_tx` → `pre_check` → `resolve_tx` → `Err(OutPointError::Unknown)` → `after_process` → `is_missing_input` → `add_orphan`.
4. After 100 transactions, orphan pool is at capacity.
5. Any legitimate orphan arriving causes `limit_size` to randomly evict one entry — the legitimate orphan has ~1% chance of immediate eviction.
6. Attacker replenishes evicted slots continuously, maintaining saturation indefinitely.

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** The orphan pool (100 slots) can be permanently saturated by a single peer at zero CKB cost. Legitimate high-fee orphan transactions are continuously at risk of random eviction, forcing re-fetch and re-relay across the network, increasing relay latency and bandwidth waste network-wide. The attacker-controlled orphans never transition to the pending pool (their parents are never submitted), providing no legitimate value while occupying all slots.

## Likelihood Explanation
The attack requires only the ability to send P2P relay messages — no CKB tokens, no privileged access, no PoW. Constructing 100 structurally valid transactions with fabricated input `OutPoint`s is trivial. The attack is self-sustaining: the attacker sends replacements as slots open, maintaining saturation indefinitely. A single malicious peer is sufficient.

## Recommendation
1. **Add a fee-rate pre-screen at orphan admission.** Before calling `add_orphan`, perform a lightweight capacity sanity check on declared outputs vs. a minimum threshold, or require a minimum `declared_cycle`-based fee estimate without needing full input resolution.
2. **Replace random eviction with fee-rate-ordered eviction.** `limit_size` should evict the entry with the lowest `fee / size` ratio (estimable from declared cycles and tx size), not a random entry.
3. **Add a per-peer orphan quota.** Limit the number of orphan slots any single `PeerIndex` can occupy (e.g., 10 out of 100) in `add_orphan_tx`.
4. **Reduce `DEFAULT_MAX_ORPHAN_TRANSACTIONS` or add a declared-fee floor.** Require that `declared_cycle` implies a minimum fee rate before admission.

## Proof of Concept
```
1. Attacker constructs 100 transactions T_1..T_100:
   - Each has one input referencing a random, non-existent OutPoint
   - Each has one output with capacity >= input's claimed capacity (zero fee — never checked)
   - Each passes NonContextualTransactionVerifier (valid version, size, non-empty, no dup deps)

2. Attacker relays T_1..T_100 to a target node via SendTransaction P2P messages.

3. For each T_i:
   - process_tx → non_contextual_verify (passes)
   - _process_tx → pre_check → resolve_tx → Err(OutPointError::Unknown)
   - try_or_return_with_snapshot! returns Some((Err(reject), snapshot))
   - after_process → is_missing_input → add_orphan(T_i, peer, declared_cycle)
   - add_orphan_tx inserts T_i; limit_size does nothing (pool not yet full)

4. After T_100: orphan pool is at capacity (100/100).

5. A legitimate node relays a high-fee orphan L:
   - add_orphan_tx inserts L (pool becomes 101)
   - limit_size evicts one entry at random — L has ~1% chance of immediate eviction

6. Attacker monitors evictions and sends T_101, T_102, ... to replace evicted slots,
   maintaining saturation indefinitely.

7. Result: orphan pool remains permanently full of attacker-controlled zero-fee orphans;
   legitimate orphans are continuously at risk of random eviction.
```

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

**File:** tx-pool/src/util.rs (L28-53)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
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
