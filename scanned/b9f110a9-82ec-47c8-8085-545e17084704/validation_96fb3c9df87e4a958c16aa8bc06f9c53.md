The code at lines 62–65 confirms the claim exactly:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
```

`self.total_keys_num` is read, `checked_add(1)` produces a new value bound to the local `total_keys_num`, but `self.total_keys_num` is never reassigned. The counter stays at its initial estimate (0 for a fresh DB) for the entire process lifetime. `shrink()` at lines 110–111 does correctly update `self.total_keys_num`, but it is never reached via the count path.

Audit Report

## Title
`RecentReject` Count Limit Never Enforced Due to Missing Write-Back of Incremented `total_keys_num` in `put()` — (File: `tx-pool/src/component/recent_reject.rs`)

## Summary
In `RecentReject::put()`, `self.total_keys_num.checked_add(1)` produces a new value bound to a local variable but never writes it back to `self.total_keys_num`. The field remains at its initial RocksDB estimate (typically `0` for a fresh database) for the entire process lifetime. Because the count-limit guard compares this stale value against `count_limit`, `shrink()` is never triggered via the count path, and the on-disk rejected-transaction database grows without bound until the TTL expires entries.

## Finding Description
**Root cause:** `tx-pool/src/component/recent_reject.rs` lines 62–65:
```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
}
```
`self.total_keys_num` is read but never reassigned. The local binding `total_keys_num` is always `initial_estimate + 1`. For a fresh database the initial estimate is `0`, so the comparison is always `1 > count_limit`, which is `false` for any `count_limit ≥ 1` (default is `keep_rejected_tx_hashes_count`, a large number).

**Initialization:** Lines 39–52 set `total_keys_num` once from `estimate_num_keys_cf` at startup and never update it again through `put()`.

**`shrink()` correctness:** Lines 110–111 do correctly write `self.total_keys_num = total_keys_num` after compaction, but `shrink()` is unreachable via the count path because the guard is never satisfied.

**Exploit flow:**
1. Attacker submits transactions guaranteed to be rejected (e.g., double-spends of a known dead cell, or transactions with invalid scripts).
2. Each rejection calls `put()`, writing one entry to the RocksDB shard.
3. `self.total_keys_num` stays at `0` (or initial estimate); `shrink()` is never called.
4. Entries accumulate until the TTL window (`keep_rejected_tx_hashes_days * 86400` seconds, default 7 days) expires them.
5. Over that window the `recent_reject` RocksDB directory grows monotonically.

**Existing guards reviewed:** The TTL is the only remaining protection. It does not bound the instantaneous disk usage within the TTL window; a sustained flood keeps the database large continuously.

## Impact Explanation
Unbounded growth of the `recent_reject` RocksDB directory can exhaust available disk space. RocksDB write failures propagate upward and can halt the tx-pool service, crashing the CKB node. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation
- Submitting rejected transactions via `send_transaction` RPC or P2P relay requires no special privilege.
- Generating syntactically valid but semantically invalid transactions (double-spends, invalid scripts) is trivial and deterministic.
- The bug is not race-dependent; every single call to `put()` fails to update the counter.
- The RPC endpoint is localhost-only by default, but P2P relay is publicly reachable. Even at moderate relay rates, sustained flooding over the 7-day TTL window can accumulate significant disk usage.
- Likelihood: **Medium**.

## Recommendation
Write the incremented value back to `self.total_keys_num` before the comparison in `put()`:
```rust
if let Some(new_total) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = new_total;   // ← missing write-back
    if self.total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```
`shrink()` already resets `self.total_keys_num` via `estimate_total_keys_num()` (lines 110–111), so no additional change is needed there.

## Proof of Concept
1. Start a CKB node with default configuration (`keep_rejected_tx_hashes_count = 10_000`, `keep_rejected_tx_hashes_days = 7`).
2. Obtain any live cell outpoint from the chain.
3. In a loop, construct a transaction that double-spends that outpoint (vary only the witness each iteration to produce a unique tx hash) and submit via `send_transaction` RPC.
4. Each submission is rejected and recorded via `put()`.
5. Verify via `get_estimate_total_keys_num()` RPC that `total_keys_num` never advances past its initial value, confirm `shrink()` is never invoked, and observe the `recent_reject` RocksDB directory size growing monotonically on disk. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** tx-pool/src/component/recent_reject.rs (L104-113)
```rust
    fn shrink(&mut self) -> Result<u64, AnyError> {
        let mut rng = thread_rng();
        let shard = rng.sample(Uniform::new(0, self.shard_num)).to_string();
        self.db.drop_cf(&shard)?;
        self.db.create_cf_with_ttl(&shard, self.ttl)?;

        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
        Ok(total_keys_num)
    }
```
