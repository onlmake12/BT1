### Title
Missing `consecutive_failed` Reset on Successful Package Inclusion Causes Premature Block-Template Termination — (`tx-pool/src/component/tx_selector.rs`)

---

### Summary

`txs_to_commit` in `TxSelector` maintains a `consecutive_failed` counter that is incremented on every size/cycles overflow or missing-ancestor failure, but is **never reset to zero when a package is successfully included**. An unprivileged attacker who floods the proposed pool with transactions whose `ancestors_size` is just below `size_limit` can cause the counter to monotonically accumulate to `MAX_CONSECUTIVE_FAILURES` (4 000), triggering an early `break` that silently discards all remaining high-fee proposed transactions from the block template.

---

### Finding Description

In `txs_to_commit`, the counter is initialized once and only ever incremented: [1](#0-0) 

It is incremented at two failure sites: [2](#0-1) [3](#0-2) 

The entire success path (lines 191–220) contains **no `consecutive_failed = 0` reset**: [4](#0-3) 

The upstream Bitcoin Core algorithm (on which this is modelled) explicitly resets `nConsecutiveFailed = 0` after every successful package commit. CKB omits that reset entirely.

The initial iterator filter only removes entries whose `ancestors_size > size_limit` (absolute limit): [5](#0-4) 

But the in-loop check uses the **running** `size` accumulator: [6](#0-5) 

So a transaction with `ancestors_size = size_limit - 1` passes the pre-filter, succeeds on the first iteration (when `size == 0`), but causes every subsequent transaction with a similar `ancestors_size` to fail once `size > 0`.

---

### Impact Explanation

Every call to `package_txs` → `txs_to_commit` is affected: [7](#0-6) 

Once `consecutive_failed` exceeds 4 000, the loop exits and `self.entries` is returned as-is, silently omitting all remaining proposed transactions regardless of their fee rate or available block space. Miners produce suboptimal (revenue-reduced) block templates. The attack persists for every block template call while the attacker's transactions remain in the proposed pool (i.e., within the CKB proposal window). The attacker can re-submit new chains each window to sustain the effect indefinitely.

---

### Likelihood Explanation

The attacker entry point is standard, unprivileged transaction submission via P2P or RPC. No mining power is required; any miner on the network will naturally include cheap proposal short-IDs. The attacker needs ~4 001 valid transactions with crafted `ancestors_size` values, which is achievable with modest on-chain fees. The bug is deterministic and locally reproducible with a unit test against `PoolMap`.

---

### Recommendation

Reset `consecutive_failed` to `0` immediately after a package is successfully committed, mirroring Bitcoin Core's behaviour. Insert the following after `self.update_modified_entries(&ancestors)` at line 220:

```rust
consecutive_failed = 0;
```

---

### Proof of Concept

```rust
// Pseudocode unit test
let size_limit: usize = 597_000;
let mut pool = PoolMap::new(DEFAULT_MAX_ANCESTORS_COUNT);

// One small "seed" tx that will succeed and push size > 0
pool.add_proposed(small_tx_entry(size: 200, ancestors_size: 200, fee: high));

// 4001 attacker txs: ancestors_size just under size_limit, own size tiny
for i in 0..4001 {
    pool.add_proposed(attacker_tx_entry(
        size: 200,
        ancestors_size: size_limit - 1,  // passes pre-filter
        fee: medium,
    ));
}

let (entries, _, _) = TxSelector::new(&pool).txs_to_commit(size_limit, u64::MAX);

// Without fix: entries.len() == 1 (only the seed tx)
// With fix (reset consecutive_failed=0 on success): entries.len() == 1 + however many fit
assert_eq!(entries.len(), 1); // demonstrates premature termination
```

The seed tx succeeds, `size` becomes 200. Each of the 4 001 attacker txs then hits `200 + (size_limit - 1) > size_limit`, incrementing `consecutive_failed` without reset. After 4 001 increments the loop breaks, and any remaining legitimate high-fee small transactions that would have fit are never considered.

### Citations

**File:** tx-pool/src/component/tx_selector.rs (L104-104)
```rust
        let mut consecutive_failed = 0;
```

**File:** tx-pool/src/component/tx_selector.rs (L109-111)
```rust
            .filter(|entry| {
                entry.ancestors_size <= size_limit && entry.ancestors_cycles <= cycles_limit
            })
```

**File:** tx-pool/src/component/tx_selector.rs (L149-162)
```rust
            let next_size = size.saturating_add(tx_entry.ancestors_size);
            let next_cycles = cycles.saturating_add(tx_entry.ancestors_cycles);

            if next_cycles > cycles_limit || next_size > size_limit {
                consecutive_failed += 1;
                if using_modified {
                    self.modified_entries.remove(&short_id);
                    self.failed_txs.insert(short_id.clone());
                }
                if consecutive_failed > MAX_CONSECUTIVE_FAILURES {
                    break;
                }
                continue;
            }
```

**File:** tx-pool/src/component/tx_selector.rs (L184-188)
```rust
                consecutive_failed += 1;
                if consecutive_failed > MAX_CONSECUTIVE_FAILURES {
                    break;
                }
                continue;
```

**File:** tx-pool/src/component/tx_selector.rs (L207-221)
```rust
            for (short_id, entry) in &ancestors {
                let is_new = self.fetched_txs.insert(short_id.clone());
                if !is_new {
                    debug!("package duplicate txs {}", short_id);
                    continue;
                }
                cycles = cycles.saturating_add(entry.cycles);
                size = size.saturating_add(entry.size);
                self.entries.push(entry.to_owned());
                // try remove from modified
                self.modified_entries.remove(short_id);
            }

            self.update_modified_entries(&ancestors);
        }
```

**File:** tx-pool/src/pool.rs (L536-554)
```rust
    pub(crate) fn package_txs(
        &self,
        max_block_cycles: Cycle,
        txs_size_limit: usize,
    ) -> (Vec<TxEntry>, usize, Cycle) {
        let (entries, size, cycles) =
            TxSelector::new(&self.pool_map).txs_to_commit(txs_size_limit, max_block_cycles);

        if !entries.is_empty() {
            ckb_logger::info!(
                "[get_block_template] candidate txs count: {}, size: {}/{}, cycles:{}/{}",
                entries.len(),
                size,
                txs_size_limit,
                cycles,
                max_block_cycles
            );
        }
        (entries, size, cycles)
```
