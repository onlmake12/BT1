Audit Report

## Title
`consecutive_failed` Never Reset on Successful Transaction Inclusion Allows Premature Block-Building Loop Termination — (`tx-pool/src/component/tx_selector.rs`)

## Summary

In `txs_to_commit`, the `consecutive_failed` counter is initialized once at line 104 and incremented on two distinct failure paths (lines 153 and 184), but is **never reset to zero when a transaction is successfully packaged** (lines 191–220). An attacker who crafts transactions such that oversized and small transactions alternate in fee-rate order can cause `consecutive_failed` to accumulate monotonically across successful inclusions, reach `MAX_CONSECUTIVE_FAILURES = 4000`, and terminate the loop while the block still has physical room for profitable transactions.

## Finding Description

The counter is initialized once: [1](#0-0) 

Two failure paths increment it: [2](#0-1) [3](#0-2) 

The entire success path (collecting ancestors, updating `size`/`cycles`, calling `update_modified_entries`) contains no assignment to `consecutive_failed`: [4](#0-3) 

The pre-loop filter admits any entry whose `ancestors_size <= size_limit`: [5](#0-4) 

Inside the loop, the size check uses `size.saturating_add(tx_entry.ancestors_size) > size_limit` (line 152). A "large" transaction with `ancestors_size` close to `size_limit` passes the pre-filter but fails the in-loop check as soon as any small transaction has been added to the block (`size >= 1`).

**Exploit construction:** Craft two independent transaction sets:

| Set | `ancestors_size` | fee rate |
|-----|-----------------|----------|
| S (small) | 1 byte | 100, 98, 96, … |
| L (large) | ≈ size_limit | 99, 97, 95, … |

`sorted_proposed_iter()` yields them in strict descending fee-rate order: S(100), L(99), S(98), L(97), …

- S(100): success; `consecutive_failed` stays 0.
- L(99): `next_size = 1 + size_limit > size_limit` → failure; `consecutive_failed = 1`.
- S(98): success; `consecutive_failed` stays **1** (not reset).
- L(97): failure; `consecutive_failed = 2`.
- …

After 4001 L-failures interleaved with 4001 S-successes, `consecutive_failed > 4000` and the loop breaks. Any remaining small transactions — which the block has physical room for — are abandoned.

Note: `failed_txs` is only populated when `using_modified = true` (line 154–156), so large transactions from the main pool iterator are not cached as failed and do not short-circuit; they each consume one loop iteration and one increment of `consecutive_failed`. [6](#0-5) 

## Impact Explanation

Miners systematically under-fill blocks. In the constructed scenario, profitable small transactions that fit within the block's remaining size and cycles budget are excluded solely because `consecutive_failed` was never reset. This reduces miner revenue per block and delays legitimate user transactions, constituting **CKB network congestion**. This matches the in-scope High impact: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation

**Entry point:** Standard `send_transaction` RPC or P2P relay — no privilege required.

**Precondition:** Transactions must reach `Status::Proposed` (CKB two-phase commit). Any miner will propose transactions with sufficient fees; the attacker's small transactions pay their own way. The large transactions require locking CKB capacity proportional to their size, which is a real but tunable cost — the attacker can reduce individual large-tx size (e.g., `ancestors_size = size_limit / k`) at the expense of needing the block to accumulate `k` small-tx bytes before each large-tx fails, adjusting the interleaving ratio while preserving the monotonic accumulation of `consecutive_failed`.

**Repeatability:** The attack applies to every block-template construction call while the adversarial transactions remain in the proposed pool.

**Detectability:** None. No log or metric exposes premature loop termination.

## Recommendation

Reset `consecutive_failed` to zero immediately after a transaction package is successfully added, matching the original Bitcoin Core intent that the counter measures an *unbroken run* of failures:

```rust
// after self.update_modified_entries(&ancestors);
consecutive_failed = 0;
``` [7](#0-6) 

## Proof of Concept

```
Pool state (all Status::Proposed):
  4001 "small" txs: ancestors_size=1,         fee_rate = 100, 98, 96, ..., 100-2*4000
  4001 "large" txs: ancestors_size=size_limit, fee_rate =  99, 97, 95, ...,  99-2*4000

sorted_proposed_iter order: S(100), L(99), S(98), L(97), ..., S(...), L(...)

Trace:
  iter 1:    S(100) → success,  consecutive_failed = 0
  iter 2:    L(99)  → failure,  consecutive_failed = 1
  iter 3:    S(98)  → success,  consecutive_failed = 1  ← not reset
  iter 4:    L(97)  → failure,  consecutive_failed = 2
  ...
  iter 8001: S(..)  → success,  consecutive_failed = 4000
  iter 8002: L(..)  → failure,  consecutive_failed = 4001 > 4000 → BREAK

Result: remaining small txs abandoned despite block having room.

Comparison:
  pool with 8002 small txs only (no large):  all ~8002 committed
  pool with 4001 small + 4001 large (above): loop breaks at 4001 small committed
  delta caused solely by missing consecutive_failed = 0 on success
```

### Citations

**File:** tx-pool/src/component/tx_selector.rs (L104-104)
```rust
        let mut consecutive_failed = 0;
```

**File:** tx-pool/src/component/tx_selector.rs (L106-112)
```rust
        let mut iter = self
            .pool_map
            .sorted_proposed_iter()
            .filter(|entry| {
                entry.ancestors_size <= size_limit && entry.ancestors_cycles <= cycles_limit
            })
            .peekable();
```

**File:** tx-pool/src/component/tx_selector.rs (L152-162)
```rust
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

**File:** tx-pool/src/component/tx_selector.rs (L184-189)
```rust
                consecutive_failed += 1;
                if consecutive_failed > MAX_CONSECUTIVE_FAILURES {
                    break;
                }
                continue;
            }
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
