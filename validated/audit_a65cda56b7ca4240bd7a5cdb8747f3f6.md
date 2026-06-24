The code confirms the claim exactly.

In `put` (lines 62–69), the `Some` branch computes `total_keys_num = self.total_keys_num + 1` as a local variable but never writes it back to `self.total_keys_num`. [1](#0-0) 

`self.total_keys_num` is only ever written at initialization in `build` (line 51) and inside `shrink` (line 111). [2](#0-1) [3](#0-2) 

Since `shrink` is only reachable when the guard fires, and the guard never fires because the counter never advances, `shrink` is never called proactively. The claim is accurate.

---

Audit Report

## Title
`RecentReject::put` Never Increments `self.total_keys_num`, Breaking the Count-Limit Shrink Guard — (File: `tx-pool/src/component/recent_reject.rs`)

## Summary
In `RecentReject::put`, the result of `self.total_keys_num.checked_add(1)` is bound to a local variable but never written back to `self.total_keys_num`. The field stays frozen at its startup-time DB estimate, so the `count_limit` guard that is supposed to invoke `shrink` never fires. The only remaining bound on the RocksDB-with-TTL store is the TTL itself, allowing an unprivileged P2P peer to grow the store unboundedly and exhaust node disk space.

## Finding Description
`RecentReject` is initialized in `build` (lines 39–52): it queries RocksDB for an estimated key count and stores it in `self.total_keys_num`. In `put` (lines 62–69):

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

The local `total_keys_num` is `self.total_keys_num + 1`, but `self.total_keys_num` is never assigned this value. Every subsequent call to `put` re-computes `initial_estimate + 1` and compares it against `count_limit`. On a fresh or recently-started node the initial estimate is 0, so the comparison is always `1 > count_limit`, which is false for any sane limit. `shrink` does correctly update `self.total_keys_num` (lines 110–111), but it is unreachable because the guard never triggers it.

Exploit path: connect as a P2P peer → relay a stream of unique transactions that fail validation (e.g., spending non-existent cells) → each rejection calls `RecentReject::put` → the broken guard never fires → the on-disk store grows unboundedly until TTL expires entries.

## Impact Explanation
The `count_limit` guard is the only proactive size bound on the `RecentReject` store. With it broken, disk consumption grows at `rejected_tx_rate × TTL`. Sustained disk exhaustion prevents the node from writing new blocks to its chain database, halting the node. This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
Any unprivileged P2P peer can relay transactions. Generating unique transactions that will be rejected requires no keys, no on-chain funds, and no special privilege — only a persistent connection. The attack is cheap, repeatable, and affects every node running this code.

## Recommendation
Assign the incremented value back to `self.total_keys_num` inside the `Some` branch:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;  // missing assignment
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

`shrink` already re-estimates and resets `self.total_keys_num` from the DB (lines 110–111), so no additional change is needed there.

## Proof of Concept
1. Start a CKB node with a fresh data directory (initial `total_keys_num` estimate = 0).
2. Connect a peer that continuously relays unique transactions with always-failure lock scripts (unique input cell references to avoid `Reject::Duplicated`).
3. Each transaction is rejected and stored via `put_recent_reject` → `RecentReject::put`.
4. Call `get_estimate_total_keys_num()` repeatedly — it always returns the initial value (0), while the actual RocksDB key count grows monotonically.
5. After inserting more than `count_limit` entries, confirm `shrink` was never called.
6. Continue until disk space is exhausted, causing the node to fail to write new chain data.

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L46-52)
```rust
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

**File:** tx-pool/src/component/recent_reject.rs (L110-111)
```rust
        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
```
