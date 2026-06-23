### Title
Unsafe Minimum Target of 1 Block in `get_fee_rate_statistics` RPC Allows Re-org-Corrupted Fee Rate Data — (File: `rpc/src/util/fee_rate.rs`)

---

### Summary

The `get_fee_rate_statistics` (and its deprecated alias `get_fee_rate_statics`) RPC enforces no meaningful minimum on the `target` parameter. `MIN_TARGET` is hardcoded to `1`, allowing any unprivileged RPC caller to request fee rate statistics computed from a single chain tip block. A single block is trivially susceptible to re-org, making the returned statistics unreliable and manipulable. No safe lower bound is enforced or documented.

---

### Finding Description

In `rpc/src/util/fee_rate.rs`, the constants governing the `get_fee_rate_statistics` RPC are:

```rust
const DEFAULT_TARGET: u64 = 21;
const MIN_TARGET: u64 = 1;
const MAX_TARGET: u64 = 101;
``` [1](#0-0) 

The `statistics()` function processes the caller-supplied `target` as follows:

```rust
pub fn statistics(&self, target: Option<u64>) -> Option<FeeRateStatistics> {
    let mut target = target.unwrap_or(DEFAULT_TARGET);
    if is_even(target) {
        target = target.saturating_add(1);
    }
    target = std::cmp::min(self.provider.max_target(), target);
    // ...
}
``` [2](#0-1) 

The function clamps `target` to at most `MAX_TARGET = 101` but applies **no lower bound beyond 1**. When `target = 1` is passed (explicitly documented as valid in the RPC spec), the `collect()` method samples only the single tip block:

```rust
let start = std::cmp::max(
    MIN_TARGET,
    tip_number.saturating_add(1).saturating_sub(target),
);
``` [3](#0-2) 

Here `MIN_TARGET = 1` is used only to prevent underflow below block 1 (genesis), **not** to enforce a minimum sample window. With `target = 1`, `start = tip_number`, so only the tip block's `BlockExt` fee data is collected.

The RPC documentation explicitly advertises `target` range as `1 - 101`: [4](#0-3) 

The implementation in `ChainRpcImpl` passes the caller value directly to `FeeRateCollector::statistics()` with no additional validation:

```rust
fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
    Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
        .statistics(target.map(Into::into)))
}
``` [5](#0-4) 

Additionally, the `ConfirmationFraction` fee estimator (used by `estimate_fee_rate`) explicitly ignores re-orgs in its `process_block` method:

```rust
// For simpfy, we assume chain reorg will not effect tx fee.
if height <= self.best_height {
    return;
}
``` [6](#0-5) 

This means that during a re-org, fee data from detached (now non-canonical) blocks is permanently baked into the estimator's `confirm_blocks_to_confirmed_txs` statistics and is never purged, while only newly attached blocks are committed:

```rust
for blk in attached_blocks {
    self.fee_estimator.commit_block(&blk);
    ...
}
``` [7](#0-6) 

---

### Impact Explanation

An unprivileged RPC caller querying `get_fee_rate_statistics` with `target=1` receives fee rate statistics derived from a single block — the current chain tip. That block:

1. **May be re-orged away**: CKB uses a longest-chain (total difficulty) fork choice rule. A tip block can be replaced at any time by a competing fork. Fee data from a re-orged block is not canonical.
2. **Is trivially manipulable**: A miner can include transactions with artificially high or low fee rates in a single block to skew the statistics, then allow that block to be orphaned.
3. **Produces highly volatile estimates**: A single block may contain zero non-cellbase transactions (returning `null`) or a non-representative sample.

Applications and wallets that use `get_fee_rate_statistics` with small `target` values to set transaction fees may:
- Set fees too low, causing transactions to be stuck or evicted from the tx-pool.
- Set fees too high, causing users to overpay.
- In time-sensitive contexts (e.g., DAO withdrawal deadlines, time-locked cells), a stuck transaction due to an underestimated fee rate can result in missed windows and financial loss.

---

### Likelihood Explanation

The entry path is fully open to any unprivileged RPC caller. The `get_fee_rate_statistics` RPC is part of the public Chain module. Passing `target=1` is explicitly documented as valid. No authentication or rate limiting is required. Any wallet, SDK, or script that queries this RPC with a small target (or that a malicious actor instructs to do so) is affected.

---

### Recommendation

1. **Enforce a safe minimum target**: Replace `MIN_TARGET = 1` with a value that reflects re-org safety. Given CKB's `CELLBASE_MATURITY` of 4 epochs (~16 hours) and the `ProposalWindow` farthest of 10 blocks, a minimum of at least 10–21 blocks is appropriate. The existing `DEFAULT_TARGET = 21` is a reasonable safe minimum.

2. **Clamp the lower bound in `statistics()`**: Add `target = std::cmp::max(SAFE_MIN_TARGET, target);` alongside the existing `std::cmp::min` for the upper bound.

3. **Address the re-org blind spot in `ConfirmationFraction`**: When `update_tx_pool_for_reorg` is called, purge or discount fee data associated with detached blocks from the estimator's statistics rather than silently ignoring them.

---

### Proof of Concept

```
# Query fee rate statistics from only the tip block (target=1)
curl -X POST http://localhost:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_fee_rate_statistics","params":["0x1"],"id":1}'
```

With `target = 0x1` (1 block), `statistics()` computes:
- `is_even(1)` → false, target stays 1
- `min(101, 1)` → 1
- `collect(1, ...)` → `start = max(1, tip - 0) = tip`
- Only `BlockExt` for the single tip block is iterated

If the tip block is subsequently re-orged (e.g., a competing fork with higher total difficulty arrives), the returned fee rate data was from a non-canonical block. Any transaction fee set using this data is based on invalid chain state.

### Citations

**File:** rpc/src/util/fee_rate.rs (L6-8)
```rust
const DEFAULT_TARGET: u64 = 21;
const MIN_TARGET: u64 = 1;
const MAX_TARGET: u64 = 101;
```

**File:** rpc/src/util/fee_rate.rs (L40-43)
```rust
        let start = std::cmp::max(
            MIN_TARGET,
            tip_number.saturating_add(1).saturating_sub(target),
        );
```

**File:** rpc/src/util/fee_rate.rs (L79-84)
```rust
    pub fn statistics(&self, target: Option<u64>) -> Option<FeeRateStatistics> {
        let mut target = target.unwrap_or(DEFAULT_TARGET);
        if is_even(target) {
            target = target.saturating_add(1);
        }
        target = std::cmp::min(self.provider.max_target(), target);
```

**File:** rpc/src/module/chain.rs (L1582-1583)
```rust
    /// * `target` - Specify the number (1 - 101) of confirmed blocks to be counted.
    ///  If the number is even, automatically add one. If not specified, defaults to 21
```

**File:** rpc/src/module/chain.rs (L2129-2132)
```rust
    fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L377-381)
```rust
    fn process_block(&mut self, height: u64, txs: impl Iterator<Item = Byte32>) {
        // For simpfy, we assume chain reorg will not effect tx fee.
        if height <= self.best_height {
            return;
        }
```

**File:** tx-pool/src/process.rs (L822-824)
```rust
        for blk in attached_blocks {
            self.fee_estimator.commit_block(&blk);
            attached.extend(blk.transactions().into_iter().skip(1));
```
