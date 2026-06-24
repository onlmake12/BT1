The code confirms the claim. In `put` (line 62), `self.total_keys_num` is never updated — only a local shadow `total_keys_num` is computed. The `shrink` method (lines 110-111) does correctly update `self.total_keys_num`, but it is never reached because the guard condition always compares the same stale initial value.

Audit Report

## Title
`RecentReject::put` Never Increments `self.total_keys_num`, Breaking the Count-Limit Shrink Guard — (File: `tx-pool/src/component/recent_reject.rs`)

## Summary
In `RecentReject::put`, `self.total_keys_num.checked_add(1)` produces a local binding that is used only for comparison but is never written back to `self.total_keys_num`. Because the field is initialized once at startup from a database estimate and never incremented thereafter, the `count_limit` guard that is supposed to invoke `shrink` never fires. The on-disk RocksDB-with-TTL store can therefore grow well beyond `count_limit`, bounded only by the TTL, allowing an unprivileged peer to drive disk exhaustion on the target node.

## Finding Description
`RecentReject` is initialized in `build` (lines 39–52 of `recent_reject.rs`), where `total_keys_num` is set once from `estimate_num_keys_cf` across all shards. [1](#0-0) 

In `put` (lines 62–69), the guard reads:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
``` [2](#0-1) 

The local `total_keys_num` is never assigned back to `self.total_keys_num`. Every call to `put` therefore compares the same frozen initial value against `count_limit`. On a fresh or recently-started node the initial estimate is 0 (or low), so `total_keys_num > self.count_limit` is permanently false and `shrink` is never called proactively.

`shrink` does correctly update `self.total_keys_num` after a drop-and-recreate of a shard (lines 110–111), but it is unreachable via the broken guard: [3](#0-2) 

The call path from the relay handler confirms that nearly every rejection type reaches `put`: [4](#0-3) 

## Impact Explanation
The `count_limit` guard is the only proactive size bound on the `RecentReject` store. With it broken, the store grows at `rejected_tx_rate × TTL × entry_size`. A sustained stream of unique rejected transactions can exhaust node disk space. Disk exhaustion prevents the node from writing new blocks to its chain database, halting the node. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
Any unprivileged P2P peer can relay transactions. Generating unique transactions that will be rejected (spending non-existent cells, always-failure scripts, fee-rate failures) requires no keys, no on-chain funds, and no special privilege. The broken guard is present on every node running this code from startup, making it universally and cheaply exploitable.

## Recommendation
Add the missing write-back in `put`:

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

`shrink` already resets `self.total_keys_num` from the post-shrink database estimate (line 111), so no additional change is needed there. [5](#0-4) 

## Proof of Concept
1. Start a CKB node with a fresh data directory (initial `total_keys_num` estimate = 0).
2. Connect a peer that continuously relays unique transactions with always-failure lock scripts (unique input cell references to avoid `Reject::Duplicated`).
3. Each transaction is rejected and stored via `put_recent_reject` → `RecentReject::put`.
4. Call `get_estimate_total_keys_num()` repeatedly — it always returns the initial value (0), while the actual RocksDB key count grows monotonically.
5. After inserting more than `count_limit` entries, confirm `shrink` has never been called.
6. Continue until disk space is exhausted, causing the node to fail to write new chain data.

A unit test can assert the invariant: after `N > count_limit` calls to `put`, `self.total_keys_num` must equal `N` (or `shrink` must have been called), which the current code violates.

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L44-51)
```rust
        let total_keys_num = Self::checked_estimate_sum(&estimate_keys_num)?;

        Ok(RecentReject {
            shard_num,
            count_limit,
            ttl,
            db,
            total_keys_num,
```

**File:** tx-pool/src/component/recent_reject.rs (L55-71)
```rust
    pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
        let hash_slice = hash.as_slice();
        let shard = self.get_shard(hash_slice).to_string();
        let reject: ckb_jsonrpc_types::PoolTransactionReject = reject.into();
        let json_string = serde_json::to_string(&reject)?;
        self.db.put(&shard, hash_slice, json_string)?;

        if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
            if total_keys_num > self.count_limit {
                self.shrink()?;
            }
        } else {
            // overflow occurred, try shrink
            self.shrink()?;
        }
        Ok(())
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

**File:** tx-pool/src/process.rs (L522-524)
```rust
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
```
