The bug claim is **confirmed by the code**. Here is the precise analysis:

## Root Cause

In `RecentReject::put` (lines 62-69), `self.total_keys_num` is **never written back** after `checked_add(1)`: [1](#0-0) 

The local binding `total_keys_num` is computed and compared against `count_limit`, but `self.total_keys_num` is never mutated in the non-shrink path. Starting from an empty DB (`self.total_keys_num = 0`), every subsequent call evaluates `0 + 1 = 1 > count_limit`, which is false for any `count_limit >= 1`. The shrink threshold is **never crossed**, so `shrink()` is never called, and `self.total_keys_num` stays frozen at its initial estimate value forever.

The only place `self.total_keys_num` is updated is inside `shrink()`: [2](#0-1) 

But since `shrink()` is gated on the broken counter, it is unreachable in normal operation.

## Realistic Impact Assessment

The TTL on the RocksDB column families provides a **partial mitigation**: entries expire after `ttl` seconds, so the DB does not grow without bound in the absolute sense — it grows until the TTL window fills up. The intended design was that `shrink()` (dropping a random shard) would cap the count at `count_limit`, but that mechanism is entirely inoperative.

The practical effect is:
- The DB can hold far more entries than `count_limit` intends (up to the TTL window's worth of rejected txs).
- An attacker submitting a sustained stream of rejected transactions (e.g., structurally valid but below min-fee-rate) will cause disk usage proportional to the TTL window × rejection rate, not bounded by `count_limit`.
- This is a **correctness/DoS bug**, not a complete unbounded-growth crash, because TTL still applies.

## Verdict

---

### Title
`RecentReject::put` never increments `self.total_keys_num`, disabling the shrink cap — (`tx-pool/src/component/recent_reject.rs`)

### Summary
The counter `self.total_keys_num` is never updated in the normal path of `RecentReject::put`, so the shrink threshold is never crossed and the RocksDB recent-reject store grows to the TTL window's capacity rather than being capped at `count_limit`.

### Finding Description
In `RecentReject::put`, after `self.db.put(...)` succeeds, the code computes `self.total_keys_num.checked_add(1)` into a local variable but never assigns it back to `self.total_keys_num`. [3](#0-2)  The field is initialized from a RocksDB estimate at startup [4](#0-3)  and is only ever updated inside `shrink()`. [5](#0-4)  Because the counter never advances, `shrink()` is never triggered, and the intended `count_limit` cap is inoperative.

### Impact Explanation
The recent-reject DB grows to the TTL window's worth of rejected transactions rather than being bounded by `count_limit`. An attacker submitting a high rate of structurally valid but fee-rejected transactions causes disk usage proportional to `rejection_rate × ttl`, not `count_limit`. This is a **bounded DoS** (bounded by TTL), not truly unbounded disk exhaustion, which reduces severity compared to the question's claim.

### Likelihood Explanation
Any unprivileged peer can submit transactions via P2P or RPC. Transactions rejected for low fee rate are a normal, unauthenticated input path. The bug is triggered on every single `put` call with no special precondition beyond a non-zero `count_limit`.

### Recommendation
Assign the result of `checked_add(1)` back to `self.total_keys_num` when no shrink is needed:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    } else {
        self.total_keys_num = total_keys_num; // <-- missing assignment
    }
} else {
    self.shrink()?;
}
```

### Proof of Concept
Call `RecentReject::put` N times (N >> `count_limit`) starting from an empty DB. Assert `self.total_keys_num == N` — the assertion fails because the counter stays at 0. Assert the on-disk CF key count stays ≤ `count_limit` — this also fails because `shrink()` was never called. Both assertions are demonstrable with the existing test infrastructure in [6](#0-5) .

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L39-52)
```rust
        let estimate_keys_num = cf_names
            .iter()
            .map(|cf| db.estimate_num_keys_cf(cf))
            .collect::<Result<Vec<_>, _>>()?;

        let total_keys_num = Self::checked_estimate_sum(&estimate_keys_num)?;

        Ok(RecentReject {
            shard_num,
            count_limit,
            ttl,
            db,
            total_keys_num,
        })
```

**File:** tx-pool/src/component/recent_reject.rs (L62-69)
```rust
        if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
            if total_keys_num > self.count_limit {
                self.shrink()?;
            }
        } else {
            // overflow occurred, try shrink
            self.shrink()?;
        }
```

**File:** tx-pool/src/component/recent_reject.rs (L104-112)
```rust
    fn shrink(&mut self) -> Result<u64, AnyError> {
        let mut rng = thread_rng();
        let shard = rng.sample(Uniform::new(0, self.shard_num)).to_string();
        self.db.drop_cf(&shard)?;
        self.db.create_cf_with_ttl(&shard, self.ttl)?;

        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
        Ok(total_keys_num)
```

**File:** tx-pool/src/component/tests/recent_reject.rs (L1-6)
```rust
use ckb_hash::blake2b_256;
use ckb_types::{core::tx_pool::Reject, packed::Byte32};

use crate::component::recent_reject::RecentReject;

#[test]
```
