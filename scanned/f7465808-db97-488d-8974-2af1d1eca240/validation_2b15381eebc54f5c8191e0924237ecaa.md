Audit Report

## Title
Unprivileged Payment Recipient Can Block RBF Fee-Bump Indefinitely via Descendant Tree Inflation — (`tx-pool/src/pool.rs`)

## Summary

`check_rbf` in `tx-pool/src/pool.rs` rejects an RBF replacement if the total descendant count of all conflicted transactions exceeds `MAX_REPLACEMENT_CANDIDATES = 100`. Any unprivileged actor who receives even one output from a victim's in-pool transaction can inflate that descendant count to ≥ 100 by submitting a fan-out tree of cheap transactions, permanently blocking the victim's ability to fee-bump via RBF for as long as the attacker resubmits the tree after eviction.

## Finding Description

**Constant and check (verified in repo):**

`MAX_REPLACEMENT_CANDIDATES = 100` is defined at line 33 of `tx-pool/src/pool.rs`. The `check_rbf` function (line 574) iterates over conflicted transactions and accumulates their descendant counts:

```rust
// lines 616–623
for conflict in conflicts.iter() {
    let descendants = self.pool_map.calc_descendants(&conflict.id);
    replace_count += descendants.len() + 1;
    if replace_count > MAX_REPLACEMENT_CANDIDATES {
        return Err(Reject::RBFRejected(format!(
            "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
            replace_count, MAX_REPLACEMENT_CANDIDATES,
        )));
    }
```

`calc_descendants` reads live mutable pool state. There is no check on *who added* those descendants or *when* they were added relative to the original transaction.

**Why the linear-chain PoC needs adjustment:**

`resource/ckb.toml` sets `max_ancestors_count = 25`. A linear chain of 100 descendants from `tx_alice` would be rejected at depth 26 (tx_alice counts as 1 ancestor). The attacker must instead use a **fan-out tree**:

- `tx_b1` spends `cell_C` (Bob's payment output from `tx_alice`); has 1 ancestor; carries ≥ 100 outputs.
- `tx_b2` … `tx_b101` each spend one output of `tx_b1`; each has 2 ancestors.

This produces 101 descendants of `tx_alice` in the pool while every transaction satisfies `max_ancestors_count ≤ 25`. The only prerequisite is that `cell_C` carries enough capacity to fund `tx_b1` with 100 outputs (each at minimum cell capacity). This is a realistic payment amount.

**Attack flow:**

1. Alice submits `tx_alice` (input: `cell_A`; outputs: `cell_B` to Alice, `cell_C` to Bob).
2. Bob submits `tx_b1` spending `cell_C` with 100 outputs, then `tx_b2`…`tx_b101` each spending one output of `tx_b1`. All pay `min_fee_rate`. All are accepted because Bob legitimately owns `cell_C`.
3. Alice submits `tx_alice2` (same input `cell_A`, higher fee) to bump her stuck transaction.
4. `check_rbf` calls `calc_descendants` on `tx_alice`'s pool entry, gets 101 descendants, computes `replace_count = 102 > 100`, and returns `Err(Reject::RBFRejected(...))`.
5. The RPC layer maps this to `RPCError::PoolRejectedRBF` (error.rs line 191) and returns error `-1111` to Alice.
6. Whenever `limit_size` evicts Bob's tree (pool.rs lines 292–328), Bob resubmits it before Alice can retry, sustaining the block indefinitely.

**RBF is active on mainnet by default:** `min_rbf_rate = 1500 > min_fee_rate = 1000` (ckb.toml lines 212–214), so `enable_rbf()` returns `true` and `check_rbf` is called on every conflicting submission.

## Impact Explanation

This is a **design flaw that can cause CKB network congestion with few costs** (High, 10001–15000 points). During high-fee periods, RBF is the primary mechanism for users to rescue stuck low-fee transactions. An attacker who is merely a payment recipient can disable this mechanism for any victim at the cost of 101 × `min_fee_rate` per eviction cycle — negligible on mainnet. Applied systematically (e.g., by a service that receives payments from many users), this blocks fee-bumping for a large fraction of in-pool transactions, preventing the fee market from clearing and worsening network congestion. The victim's funds are not stolen but are effectively frozen in an unconfirmable pool transaction for the duration of the attack.

## Likelihood Explanation

- **Attacker role**: ordinary payment recipient — no keys, no admin access, no hashpower required.
- **Cost**: 101 minimum-fee transactions per cycle; at `min_fee_rate = 1000 shannons/KB` and typical transaction sizes, this is on the order of tens of thousands of shannons per cycle.
- **Repeatability**: the attacker monitors the pool (via `get_raw_tx_pool` RPC) and resubmits the fan-out tree immediately after eviction.
- **Precondition**: the victim's transaction must have at least one output the attacker controls — a completely normal condition for any payment.
- **RBF enabled**: confirmed active in the default mainnet configuration.

## Recommendation

1. **Do not count descendants that were added after the conflicted transaction and whose only ancestry path runs through outputs the original sender did not control.** Specifically, when evaluating Rule #5, exclude descendants whose root ancestor (the direct child of the conflicted tx) spends an output that is *not* one of the conflicted transaction's inputs.
2. **Allow the RBF submitter to evict low-fee descendants** of the conflicted transaction as part of the replacement, rather than hard-rejecting the replacement. Bitcoin Core's RBF Rule #5 permits this: the replacement may evict conflicted transactions and their descendants provided the replacement pays sufficient incremental fees.
3. **Short-term mitigation**: cap the number of unconfirmed descendants any single output can have (a per-output descendant limit), making the fan-out tree attack infeasible without a proportionally large fee investment.

## Proof of Concept

```
# Prerequisites: CKB node with default ckb.toml (min_rbf_rate=1500 > min_fee_rate=1000)

Step 1 — Alice submits tx_alice:
  send_transaction({
    inputs:  [cell_A],          # owned by Alice
    outputs: [cell_B, cell_C],  # cell_B → Alice (change), cell_C → Bob (payment)
    fee:     min_fee_rate
  })

Step 2 — Bob builds fan-out tree (101 descendants, each ≤ 2 ancestors):
  send_transaction(tx_b1) {
    inputs:  [cell_C],
    outputs: [out_1, out_2, ..., out_100],   # 100 outputs
    fee:     min_fee_rate
  }
  for i in 1..=100:
    send_transaction(tx_b_{i+1}) {
      inputs:  [out_i from tx_b1],
      outputs: [new_cell_i],
      fee:     min_fee_rate
    }
  # All accepted; each tx has ≤ 2 ancestors (tx_alice + tx_b1)

Step 3 — Alice attempts RBF:
  send_transaction(tx_alice2) {
    inputs:  [cell_A],          # same input as tx_alice
    outputs: [cell_B'],         # higher fee
  })

Step 4 — Result:
  Error -1111 (PoolRejectedRBF):
  "RBFRejected: Tx conflict with too many txs,
   conflict txs count: 102, expect <= 100"

Step 5 — Sustain:
  Bob watches get_raw_tx_pool; whenever his tree is evicted by limit_size,
  he resubmits tx_b1 and tx_b2..tx_b101 before Alice can retry.
  Alice's tx_alice remains stuck indefinitely.
```

The root cause is at `tx-pool/src/pool.rs` lines 616–623: `calc_descendants` reads attacker-writable mutable pool state with no authorship check, and the hard rejection at `replace_count > MAX_REPLACEMENT_CANDIDATES` provides no escape hatch for the legitimate transaction owner.