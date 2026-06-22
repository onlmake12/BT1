### Title
Fee Rate Estimation Uses Individual Tx Size Instead of Ancestor Package Size, Causing Systematic Underestimation - (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

`estimate_fee_rate` in `tx-pool/src/component/pool_map.rs` iterates over pool entries and accumulates `entry.inner.size` and `entry.inner.cycles` to simulate block filling. It uses the **individual transaction's** size/cycles rather than the **ancestor-package** size/cycles (`ancestors_size` / `ancestors_cycles`). Because a transaction with unconfirmed ancestors cannot be committed to a block without also committing those ancestors, the function systematically underestimates how much block space each entry actually consumes, and therefore returns a fee rate that is too low.

---

### Finding Description

`estimate_fee_rate` simulates filling `target_blocks` blocks by walking pool entries in descending score order and accumulating per-entry size and cycles:

```rust
// tx-pool/src/component/pool_map.rs  lines 342–356
let iter = self.entries.iter_by_score().rev();
let mut current_block_bytes = 0;
let mut current_block_cycles = 0;
for entry in iter {
    current_block_bytes += entry.inner.size;      // ← individual tx only
    current_block_cycles += entry.inner.cycles;   // ← individual tx only
    if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
        target_blocks -= 1;
        if target_blocks == 0 {
            return entry.inner.fee_rate();
        }
        current_block_bytes = entry.inner.size;
        current_block_cycles = entry.inner.cycles;
    }
}
``` [1](#0-0) 

By contrast, the actual block-assembly path in `TxSelector::txs_to_commit` correctly uses the **package** totals:

```rust
// tx-pool/src/component/tx_selector.rs  lines 149–152
let next_size   = size.saturating_add(tx_entry.ancestors_size);
let next_cycles = cycles.saturating_add(tx_entry.ancestors_cycles);
if next_cycles > cycles_limit || next_size > size_limit { … }
``` [2](#0-1) 

`ancestors_size` is the sum of the transaction's own size plus all its unconfirmed ancestors' sizes; it is maintained incrementally via `add_ancestor_weight` / `sub_ancestor_weight`: [3](#0-2) 

When the mempool contains CPFP chains (a common pattern where a high-fee child "sponsors" a low-fee parent), `entry.inner.size` may be, e.g., 200 bytes while `entry.inner.ancestors_size` is 600 bytes. `estimate_fee_rate` counts only 200 bytes toward block fullness, so it believes three times as many transactions fit in a block as actually do, and returns a fee rate that is proportionally too low.

---

### Impact Explanation

The RPC endpoint `estimate_fee_rate` (exposed in `rpc/src/module/experiment.rs`) is the primary consumer of this function. [4](#0-3) 

Any unprivileged RPC caller who queries this endpoint receives a fee rate that is lower than what is actually required to be included within the requested number of blocks. In a mempool with deep CPFP chains the underestimation is proportional to the average ancestor-chain depth. Concretely:

- Users who follow the estimate will set fees too low and have transactions stuck or delayed.
- A malicious actor can deliberately flood the mempool with long CPFP chains to drive the estimate arbitrarily close to `min_fee_rate`, causing widespread fee underpayment by wallets and tooling that rely on this RPC.

---

### Likelihood Explanation

CPFP chains are a normal and frequent occurrence on CKB (wallets use them to bump stuck transactions). The bug is therefore triggered under ordinary mempool conditions, not only under adversarial ones. Any RPC caller—wallet, DApp, exchange—that calls `estimate_fee_rate` is affected. No special privilege or key material is required.

---

### Recommendation

Replace `entry.inner.size` / `entry.inner.cycles` with `entry.inner.ancestors_size` / `entry.inner.ancestors_cycles` in `estimate_fee_rate`, mirroring the logic already used in `TxSelector::txs_to_commit`. The fee rate to return at the block boundary should also be the **package fee rate** (total ancestors fee / total ancestors weight), not the individual entry's fee rate.

---

### Proof of Concept

1. Populate the mempool with a chain of N transactions where tx₁ has a low fee and tx_N has a high fee (CPFP pattern). Each tx has size S; `ancestors_size` of tx_N = N × S.
2. Call `estimate_fee_rate(1, max_block_bytes, max_block_cycles, min_fee_rate)` via RPC.
3. Observe that the function counts only S bytes per entry instead of N × S, so it believes N times more transactions fit in the next block than actually do.
4. The returned fee rate is approximately `1/N` of the correct value.
5. Submit a new transaction using the returned fee rate; observe it is not included in the next block despite the estimate promising inclusion.

The discrepancy between `entry.inner.size` (line 346) and `entry.inner.ancestors_size` (used correctly at `tx_selector.rs` line 149) is the sole root cause. [5](#0-4) [6](#0-5)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L334-359)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
    }
```

**File:** tx-pool/src/component/tx_selector.rs (L149-152)
```rust
            let next_size = size.saturating_add(tx_entry.ancestors_size);
            let next_cycles = cycles.saturating_add(tx_entry.ancestors_cycles);

            if next_cycles > cycles_limit || next_size > size_limit {
```

**File:** tx-pool/src/component/entry.rs (L144-154)
```rust
    /// Update ancestor state for add an entry
    pub fn add_ancestor_weight(&mut self, entry: &TxEntry) {
        self.ancestors_count = self.ancestors_count.saturating_add(1);
        self.ancestors_size = self.ancestors_size.saturating_add(entry.size);
        self.ancestors_cycles = self.ancestors_cycles.saturating_add(entry.cycles);
        self.ancestors_fee = Capacity::shannons(
            self.ancestors_fee
                .as_u64()
                .saturating_add(entry.fee.as_u64()),
        );
    }
```
