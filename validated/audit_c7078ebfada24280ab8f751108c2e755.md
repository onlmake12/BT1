### Title
Integer Division Rounding in `threshold_number` Lowers Softfork Activation Threshold Below Configured Ratio — (`File: spec/src/versionbits/mod.rs`)

---

### Summary

The `threshold_number` function in CKB's versionbits softfork deployment logic uses integer (floor) division to compute the minimum number of signaling blocks required for a softfork to lock in. Because integer division truncates, the computed threshold is sometimes one block lower than the true ceiling of `total × numer / denom`. This allows a softfork to lock in with a signaling ratio strictly less than the configured threshold, violating the protocol's activation guarantee.

---

### Finding Description

The function `threshold_number` in `spec/src/versionbits/mod.rs` computes the minimum block count needed to lock in a softfork:

```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_div(threshold.denom()))
}
``` [1](#0-0) 

This computes `floor(total × numer / denom)`. The result is then used in the `ThresholdState::Started` branch of `get_state`:

```rust
let threshold_number = threshold_number(total, self.threshold())?;
if count >= threshold_number {
    next_state = ThresholdState::LockedIn;
}
``` [2](#0-1) 

The `threshold` field of a `Deployment` is a `Ratio` (numerator/denominator) representing the minimum fraction of blocks per period that must signal for lock-in: [3](#0-2) 

The correct minimum count to enforce "at least `threshold` fraction" is `ceil(total × numer / denom)`. Floor division produces a value that is one less than the ceiling whenever `total × numer` is not exactly divisible by `denom`. At that exact boundary, a softfork locks in with a signaling ratio strictly below the configured threshold.

**Concrete example:**

| Parameter | Value |
|---|---|
| `threshold` | 3/4 (75%) |
| `total` blocks in period | 5 |
| Exact threshold | 5 × 3/4 = 3.75 → requires **4** blocks (ceil) |
| `threshold_number` result | 5 × 3 / 4 = 15 / 4 = **3** (floor) |
| Effective ratio enforced | 3/5 = **60%** |

With epoch length 5 and threshold 3/4, a softfork locks in with only 3 signaling blocks (60%) instead of the required 4 (75%). The error is always at most 1 block, but the relative impact grows as epoch lengths shrink (e.g., in testnet or dev configurations).

The `total` variable accumulates block counts across `period` epochs: [4](#0-3) 

Because CKB epoch lengths vary dynamically (via the difficulty adjustment algorithm), `total × numer` will frequently not be divisible by `denom`, making this rounding error the common case rather than the exception.

---

### Impact Explanation

The softfork activation threshold is a consensus-critical parameter. If `threshold_number` returns a value one less than the true ceiling, all nodes independently compute the same (incorrect) threshold and agree on lock-in — so there is no chain split. However, the protocol guarantee that "at least `threshold` fraction of blocks must signal" is violated. A softfork can lock in with a signaling ratio strictly below the configured threshold, undermining the governance mechanism for consensus rule changes. For small epoch lengths (testnet, dev chains, or dynamically shortened epochs), the relative error can be significant (e.g., 60% instead of 75% as shown above).

---

### Likelihood Explanation

This triggers whenever `total × numer` is not exactly divisible by `denom`. Given that `total` is the sum of block counts across `period` epochs and epoch lengths vary dynamically, this condition is met in the vast majority of signaling periods. Any miner submitting blocks with the version bit set (an unprivileged, normal mining operation) can trigger this path. No special access or coordination is required.

---

### Recommendation

Replace floor division with ceiling division in `threshold_number`:

```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .map(|ret| ret.div_ceil(threshold.denom()))
}
```

`div_ceil` computes `ceil(a / b) = (a + b - 1) / b`, ensuring the threshold is never rounded below the configured ratio. [1](#0-0) 

---

### Proof of Concept

Using the test infrastructure in `spec/src/tests/versionbits.rs` with a deployment configured as:

```
threshold = Ratio::new(3, 4)   // 75%
epoch_length = 5
period = 1
```

`threshold_number(5, Ratio::new(3, 4))` returns `5 * 3 / 4 = 3` (floor).

The `count >= threshold_number` check passes with `count = 3` (60% signaling), even though the configured threshold is 75% (requiring `count = 4`). The softfork transitions to `ThresholdState::LockedIn` one block short of the intended threshold. [5](#0-4) [6](#0-5)

### Citations

**File:** spec/src/versionbits/mod.rs (L138-141)
```rust
    /// Specifies the minimum ratio of block per `period`,
    /// which indicate the locked_in of the softfork during the `period`.
    pub threshold: Ratio,
}
```

**File:** spec/src/versionbits/mod.rs (L326-340)
```rust
                    for _ in 0..period {
                        let current_epoch_length = current_epoch_ext.length();
                        total += current_epoch_length;
                        for _ in 0..current_epoch_length {
                            if self.condition(&header, indexer) {
                                count += 1;
                            }
                            header = indexer.block_header(&header.parent_hash())?;
                        }
                        let last_block_header_in_previous_epoch = indexer
                            .block_header(&current_epoch_ext.last_block_hash_in_previous_epoch())?;
                        let previous_epoch_index = indexer
                            .block_epoch_index(&last_block_header_in_previous_epoch.hash())?;
                        current_epoch_ext = indexer.epoch_ext(&previous_epoch_index)?;
                    }
```

**File:** spec/src/versionbits/mod.rs (L342-347)
```rust
                    let threshold_number = threshold_number(total, self.threshold())?;
                    if count >= threshold_number {
                        next_state = ThresholdState::LockedIn;
                    } else if epoch_ext.number() >= timeout {
                        next_state = ThresholdState::Failed;
                    }
```

**File:** spec/src/versionbits/mod.rs (L475-479)
```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_div(threshold.denom()))
}
```

**File:** spec/src/tests/versionbits.rs (L216-229)
```rust
    let test_dummy = Deployment {
        bit: 1,
        start: 3,
        timeout: 11,
        min_activation_epoch: 11,
        period: 2,
        active_mode: ActiveMode::Normal,
        threshold: TESTNET_ACTIVATION_THRESHOLD,
    };
    deployments.insert(DeploymentPos::Testdummy, test_dummy);

    let consensus = ConsensusBuilder::new(genesis, epoch_ext)
        .softfork_deployments(deployments)
        .build();
```
