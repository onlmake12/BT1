Audit Report

## Title
`RecentReject::put()` silently destroys just-written entry when `shrink()` drops the same shard — (`tx-pool/src/component/recent_reject.rs`)

## Summary
`put()` writes an entry to a deterministically-assigned shard, then calls `shrink()` based on a counter that is never incremented, causing `shrink()` to fire on every call once the DB reaches `count_limit`. `shrink()` randomly drops one of `shard_num` column families; if it selects the same shard just written, the entry is permanently destroyed while `put()` returns `Ok(())`.

## Finding Description

**Missing counter update confirmed.** In `put()` at lines 62–69, `self.total_keys_num.checked_add(1)` produces a local binding `total_keys_num` used only for the comparison. `self.total_keys_num` is never assigned the incremented value. It is only refreshed inside `shrink()` at line 111 via `estimate_total_keys_num()`. [1](#0-0) 

Because `self.total_keys_num` is never incremented in `put()`, once it reaches or exceeds `count_limit` (either from startup estimate or after a prior shrink that left the DB still full), the condition `total_keys_num > self.count_limit` is permanently true and `shrink()` fires on every subsequent `put()`.

**`shrink()` randomly destroys a shard.** Lines 105–108 select a shard uniformly at random and call `drop_cf` followed by `create_cf_with_ttl`, wiping all entries in that shard: [2](#0-1) 

**Shard assignment is deterministic and attacker-observable.** `get_shard()` computes `u32::from_le_bytes(hash[0..4]) % shard_num`, so an attacker can craft a transaction hash that maps to any target shard: [3](#0-2) 

**Invariant-breaking sequence:**
1. `put()` writes entry to shard X (line 60).
2. Stale `self.total_keys_num >= count_limit` → `shrink()` is called (lines 62–64).
3. `shrink()` samples a random shard; with probability `1/shard_num` it selects shard X.
4. `drop_cf(X)` destroys all data in X; `create_cf_with_ttl(X, ttl)` recreates it empty.
5. `put()` returns `Ok(())` — caller observes success.
6. Subsequent `get()` on the same hash returns `None`.

**Existing test does not cover this path.** The test in `tx-pool/src/component/tests/recent_reject.rs` uses `limit = 100` and puts only 80 entries. Since `total_keys_num` starts at 0 (empty DB) and is never incremented, `0 + 1 > 100` is always false — `shrink()` is never called during the test, and the bug is never exercised: [4](#0-3) 

## Impact Explanation

The recent-reject store is the authoritative record of rejected transactions. Silent entry loss causes `get_transaction` RPC to return `TxStatus::Unknown` instead of `TxStatus::Rejected`, and allows a previously-rejected transaction to be re-submitted and re-verified without the node recognizing it as previously rejected. This constitutes a **suboptimal implementation of the CKB state storage mechanism** (Medium, 2001–10000 points): a critical tx-pool component fails to reliably persist entries due to a missing counter update and an unguarded post-write shrink.

## Likelihood Explanation

The trigger condition is met on any active node whose DB already holds `>= keep_rejected_tx_hashes_count` rejected transactions at startup — a routine operational state. Once triggered, every `put()` call fires `shrink()`. With `DEFAULT_SHARDS = 5`, the probability of destroying the just-written entry is 20% per `put()`. An attacker who can submit rejected transactions via P2P relay (no special privilege required) can craft hashes targeting a specific shard and repeatedly trigger this condition. With `shard_num = 2`, the probability rises to 50% per attempt.

## Recommendation

1. **Increment `self.total_keys_num` in `put()`** after a successful write:
   ```rust
   self.total_keys_num = self.total_keys_num.saturating_add(1);
   ```
2. **Call `shrink()` before writing**, not after, so the just-written entry is never in the shard that may be dropped.
3. Alternatively, **exclude the just-written shard from `shrink()`'s random selection** by passing it as a parameter and retrying if the random pick matches.

## Proof of Concept

```rust
// shard_num=2, count_limit=1, ttl=-1
let tmp = tempfile::tempdir().unwrap();
let mut rr = RecentReject::build(tmp.path(), 2, 1, -1).unwrap();

// Force total_keys_num to count_limit so shrink fires on every put
rr.total_keys_num = rr.count_limit; // fields are pub(crate), accessible in tests

// Craft a hash that maps to shard 0 (first 4 bytes LE % 2 == 0)
let hash = Byte32::new([0u8; 32]); // 0 % 2 == 0 → shard "0"

// put() writes to shard 0, then shrink() randomly drops shard 0 or 1
rr.put(&hash, Reject::Malformed("x".into(), Default::default())).unwrap();

// With 50% probability per attempt, get() returns None after Ok put
// Reliable within ~3 iterations
assert!(rr.get(&hash).unwrap().is_none()); // invariant violated
```

The `pub(crate)` visibility of `total_keys_num` and `count_limit` (lines 15–16) and the `pub(crate)` visibility of `db` (line 17) confirm the PoC fields are accessible from within the crate's test module. [5](#0-4)

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L12-18)
```rust
pub struct RecentReject {
    ttl: i32,
    shard_num: u32,
    pub(crate) count_limit: u64,
    pub(crate) total_keys_num: u64,
    pub(crate) db: DBWithTTL,
}
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

**File:** tx-pool/src/component/recent_reject.rs (L115-119)
```rust
    fn get_shard(&self, hash: &[u8]) -> u32 {
        let mut low_u32 = [0u8; 4];
        low_u32.copy_from_slice(&hash[0..4]);
        u32::from_le_bytes(low_u32) % self.shard_num
    }
```

**File:** tx-pool/src/component/tests/recent_reject.rs (L7-39)
```rust
fn test_basic() {
    let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
    let shard_num = 2;
    let limit = 100;
    let ttl = -1;

    let mut recent_reject = RecentReject::build(tmp_dir.path(), shard_num, limit, ttl).unwrap();

    for i in 0..80u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        recent_reject
            .put(&key, Reject::Malformed(i.to_string(), Default::default()))
            .unwrap();
    }

    for i in 0..80u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        let reject: ckb_jsonrpc_types::PoolTransactionReject =
            Reject::Malformed(i.to_string(), Default::default()).into();
        assert_eq!(
            recent_reject.get(&key).unwrap().unwrap(),
            serde_json::to_string(&reject).unwrap()
        )
    }

    for i in 0..80u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        recent_reject
            .put(&key, Reject::Malformed(i.to_string(), Default::default()))
            .unwrap();
    }

    assert!(recent_reject.total_keys_num < 100);
```
