### Title
Integer Overflow in `historical_blocks()` Causes Division-by-Zero Panic in Fee Estimator — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

### Summary

The `weight_units_flow` fee estimator's `do_estimate` function accepts a user-supplied `target_blocks` parameter that is passed without validation to `historical_blocks()`, which multiplies it by 2 without overflow protection. When `target_blocks = 2^63`, the multiplication wraps to zero in release mode, bypassing the guard check that is supposed to reject out-of-range inputs. Execution then reaches an unconditional integer division `value / historical_blocks` where `historical_blocks = 0`, causing a division-by-zero panic reachable by any unprivileged RPC caller.

### Finding Description

In `util/fee-estimator/src/estimator/weight_units_flow.rs`, the private helper `historical_blocks` computes the look-back window as a plain unchecked multiplication:

```rust
fn historical_blocks(target_blocks: BlockNumber) -> BlockNumber {
    if target_blocks < constants::MIN_TARGET {
        constants::MIN_TARGET * 2
    } else {
        target_blocks * 2          // ← no overflow check
    }
}
``` [1](#0-0) 

In Rust release builds, integer overflow wraps silently. When `target_blocks = 2^63` (= `0x8000_0000_0000_0000`), `target_blocks * 2` wraps to `0`.

`do_estimate` then runs the guard:

```rust
let historical_blocks = Self::historical_blocks(target_blocks);
if historical_blocks > self.current_tip.saturating_sub(self.boot_tip) {
    return Err(Error::LackData);
}
``` [2](#0-1) 

`0 > anything` is always `false`, so the guard is silently bypassed. Execution continues to the flow-speed bucket computation:

```rust
buckets
    .into_iter()
    .map(|value| value / historical_blocks)   // historical_blocks == 0 → PANIC
    .collect::<Vec<_>>()
``` [3](#0-2) 

Integer division by zero always panics in Rust regardless of build profile. The panic is triggered whenever the tx-pool is non-empty (so `sorted_current_txs` is non-empty and `max_bucket_index > 0`).

The public entry point is `Algorithm::estimate_fee_rate`, which accepts `target_blocks: BlockNumber` directly from the caller and performs no range validation before forwarding to `do_estimate`:

```rust
pub fn estimate_fee_rate(
    &self,
    target_blocks: BlockNumber,
    all_entry_info: TxPoolEntryInfo,
) -> Result<FeeRate, Error> {
    if !self.is_ready { return Err(Error::NotReady); }
    ...
    self.do_estimate(target_blocks, &sorted_current_txs)
}
``` [4](#0-3) 

### Impact Explanation

Any unprivileged RPC caller who can invoke the fee-estimation endpoint can trigger a division-by-zero panic in the fee estimator service with a single crafted request (`target_blocks = 0x8000000000000000`). Depending on how the RPC layer handles panics (tokio task abort vs. process-level unwind), the consequence ranges from a failed RPC response to a crash of the node process. At minimum, the panic terminates the handling task and disrupts fee estimation for all concurrent callers; at worst it brings down the node, constituting a remotely-triggerable denial-of-service.

### Likelihood Explanation

The attack requires no credentials, no stake, and no special network position. A single HTTP POST to the fee-estimation RPC endpoint with a crafted `target_blocks` value is sufficient. The condition is deterministic and reproducible on any node with a non-empty mempool.

### Recommendation

1. Replace the bare multiplication in `historical_blocks` with a checked or saturating variant:
   ```rust
   fn historical_blocks(target_blocks: BlockNumber) -> BlockNumber {
       target_blocks.saturating_mul(2).max(constants::MIN_TARGET * 2)
   }
   ```
2. Add an explicit upper-bound check on `target_blocks` at the RPC entry point (e.g., reject values exceeding `MAX_TARGET`).
3. Guard the division site: if `historical_blocks == 0`, return `Err(Error::LackData)` rather than dividing.

### Proof of Concept

**Precondition**: node is running with at least one transaction in the mempool (so `sorted_current_txs` is non-empty).

**Trigger**:
```
POST / HTTP/1.1
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "method": "estimate_fee_rate",
  "params": { "target_blocks": "0x8000000000000000" },
  "id": 1
}
```

**Execution trace**:
1. `target_blocks = 0x8000_0000_0000_0000` (= 2^63)
2. `historical_blocks(2^63)` → `2^63 * 2` wraps to `0` in release mode
3. Guard: `0 > current_tip - boot_tip` → `false` → not returned
4. `value / 0` → **panic: attempt to divide by zero** [5](#0-4)

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L164-185)
```rust
    pub fn estimate_fee_rate(
        &self,
        target_blocks: BlockNumber,
        all_entry_info: TxPoolEntryInfo,
    ) -> Result<FeeRate, Error> {
        if !self.is_ready {
            return Err(Error::NotReady);
        }

        let sorted_current_txs = {
            let mut current_txs: Vec<_> = all_entry_info
                .pending
                .into_values()
                .chain(all_entry_info.proposed.into_values())
                .map(TxStatus::new_from_entry_info)
                .collect();
            current_txs.sort_unstable_by(|a, b| b.cmp(a));
            current_txs
        };

        self.do_estimate(target_blocks, &sorted_current_txs)
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L199-203)
```rust
        let historical_blocks = Self::historical_blocks(target_blocks);
        ckb_logger::debug!("required: {historical_blocks} blocks");
        if historical_blocks > self.current_tip.saturating_sub(self.boot_tip) {
            return Err(Error::LackData);
        }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L268-272)
```rust
            buckets
                .into_iter()
                .map(|value| value / historical_blocks)
                .collect::<Vec<_>>()
        };
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L279-285)
```rust
        for bucket_index in 1..=max_bucket_index {
            let current_weight = current_weight_buckets[bucket_index];
            let added_weight = flow_speed_buckets[bucket_index] * target_blocks;
            // Note: blocks are not full even there are many pending transactions,
            // since `MAX_BLOCK_PROPOSALS_LIMIT = 1500`.
            let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
            let passed = current_weight + added_weight <= removed_weight;
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L316-323)
```rust
impl Algorithm {
    fn historical_blocks(target_blocks: BlockNumber) -> BlockNumber {
        if target_blocks < constants::MIN_TARGET {
            constants::MIN_TARGET * 2
        } else {
            target_blocks * 2
        }
    }
```
