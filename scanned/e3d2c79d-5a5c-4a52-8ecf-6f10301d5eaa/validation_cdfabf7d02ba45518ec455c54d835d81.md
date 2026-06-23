### Title
Integer Division Truncation in `threshold_number` Can Zero Out Softfork Activation Threshold — (`spec/src/versionbits/mod.rs`)

### Summary

The `threshold_number` function in `spec/src/versionbits/mod.rs` computes the minimum number of signaling blocks required to lock in a softfork using integer division: `(total * numer) / denom`. When `total * numer < denom`, integer truncation produces `0`. Because the comparison is `count >= threshold_number` where `count` is a `u64`, a threshold of `0` is always satisfied — meaning a softfork can transition to `LockedIn` with **zero** miner signaling blocks.

### Finding Description

The private function `threshold_number` is the sole gating computation for RFC0043 softfork lock-in:

```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_div(threshold.denom()))
}
``` [1](#0-0) 

Its result is used directly in the `ThresholdState::Started` branch of `get_state`:

```rust
let threshold_number = threshold_number(total, self.threshold())?;
if count >= threshold_number {
    next_state = ThresholdState::LockedIn;
}
``` [2](#0-1) 

The `Ratio` type stores `numer` and `denom` as raw `u64` values with **no validation** — no check that `numer <= denom`, no check that `denom != 0`, and no minimum floor on the computed result:

```rust
pub const fn new(numer: u64, denom: u64) -> Self {
    Self { numer, denom }
}
``` [3](#0-2) 

The `Deployment` struct accepts any `Ratio` as its `threshold` field without further validation: [4](#0-3) 

### Impact Explanation

When `threshold_number` returns `Some(0)`, the condition `count >= 0` is trivially true for any `u64` value of `count` (including `count = 0`). The softfork state machine transitions to `ThresholdState::LockedIn` in the very first signaling period, regardless of how many miners actually signaled. This bypasses the entire RFC0043 miner-signaling consensus mechanism, allowing a protocol rule change to activate without the required miner supermajority.

The cached `LockedIn` state is then persisted:

```rust
cache.insert(&epoch_ext.last_block_hash_in_previous_epoch(), state);
``` [5](#0-4) 

Once cached as `LockedIn`, the state is irreversible (it is a terminal progression toward `Active`).

### Likelihood Explanation

The current production mainnet and testnet deployments map is empty:

```rust
mainnet::CHAIN_SPEC_NAME => {
    let deployments = HashMap::new();
    Some(deployments)
}
testnet::CHAIN_SPEC_NAME => {
    let deployments = HashMap::new();
    Some(deployments)
}
``` [6](#0-5) 

The dev-chain `LightClient` deployment uses `ActiveMode::Always`, which short-circuits before `threshold_number` is ever called: [7](#0-6) 

The two defined threshold constants — `LC_MAINNET_ACTIVATION_THRESHOLD = Ratio::new(8, 10)` and `TESTNET_ACTIVATION_THRESHOLD = Ratio::new(3, 4)` — are large enough that with the minimum epoch length of 300 blocks, `threshold_number` would not return 0: [8](#0-7) 

However, any **future RFC0043 deployment** that uses a threshold with a large denominator relative to its numerator (e.g., `Ratio::new(1, 1000)`) would produce `threshold_number = 0` for any realistic epoch length, silently activating the softfork with zero signaling. The `Ratio` constructor enforces no such constraint, making this a latent but structurally guaranteed defect for future deployments.

### Recommendation

1. **Add a floor of 1** to `threshold_number` so it never returns `Some(0)`:

```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_div(threshold.denom()))
        .map(|n| cmp::max(n, 1))
}
```

2. **Validate `Ratio` at construction time** for deployment thresholds: assert `numer > 0`, `denom > 0`, and `numer <= denom`.

3. **Add a consensus-level check** when registering a `Deployment` that `threshold_number(MIN_EPOCH_LENGTH, deployment.threshold)` is at least 1.

### Proof of Concept

With a deployment configured as `threshold: Ratio::new(1, 1000)` and `period = 1`:

- Minimum epoch length = 300 blocks
- `total = 300`
- `threshold_number(300, Ratio::new(1, 1000))` = `300 * 1 / 1000` = `0` (integer truncation)
- `count >= 0` is always `true` for `u64`
- Softfork transitions to `LockedIn` in the first signaling period with **zero** signaling blocks

The same truncation occurs for `Ratio::new(1, 500)` with `total = 300`: `300 * 1 / 500 = 0`. Any threshold whose numerator is less than `denom / total` silently becomes a zero threshold, completely defeating the RFC0043 miner-signaling safety mechanism. [9](#0-8)

### Citations

**File:** spec/src/versionbits/mod.rs (L138-141)
```rust
    /// Specifies the minimum ratio of block per `period`,
    /// which indicate the locked_in of the softfork during the `period`.
    pub threshold: Ratio,
}
```

**File:** spec/src/versionbits/mod.rs (L316-347)
```rust
                ThresholdState::Started => {
                    // We need to count
                    debug_assert!(epoch_ext.number() + 1 >= period);

                    let mut count = 0;
                    let mut total = 0;
                    let mut header =
                        indexer.block_header(&epoch_ext.last_block_hash_in_previous_epoch())?;

                    let mut current_epoch_ext = epoch_ext.clone();
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

                    let threshold_number = threshold_number(total, self.threshold())?;
                    if count >= threshold_number {
                        next_state = ThresholdState::LockedIn;
                    } else if epoch_ext.number() >= timeout {
                        next_state = ThresholdState::Failed;
                    }
```

**File:** spec/src/versionbits/mod.rs (L359-359)
```rust
            cache.insert(&epoch_ext.last_block_hash_in_previous_epoch(), state);
```

**File:** spec/src/versionbits/mod.rs (L475-479)
```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_div(threshold.denom()))
}
```

**File:** util/occupied-capacity/core/src/units.rs (L22-25)
```rust
    /// Creates a ratio numer / denom.
    pub const fn new(numer: u64, denom: u64) -> Self {
        Self { numer, denom }
    }
```

**File:** spec/src/lib.rs (L531-539)
```rust
        match self.name.as_str() {
            mainnet::CHAIN_SPEC_NAME => {
                let deployments = HashMap::new();
                Some(deployments)
            }
            testnet::CHAIN_SPEC_NAME => {
                let deployments = HashMap::new();
                Some(deployments)
            }
```

**File:** spec/src/lib.rs (L541-553)
```rust
                let mut deployments = HashMap::new();
                let light_client = Deployment {
                    bit: 1,
                    start: 0,
                    timeout: 0,
                    min_activation_epoch: 0,
                    period: 10,
                    active_mode: ActiveMode::Always,
                    threshold: TESTNET_ACTIVATION_THRESHOLD,
                };
                deployments.insert(DeploymentPos::LightClient, light_client);
                Some(deployments)
            }
```

**File:** spec/src/consensus.rs (L98-101)
```rust
/// The mainnet default activation_threshold
pub const LC_MAINNET_ACTIVATION_THRESHOLD: Ratio = Ratio::new(8, 10);
/// The testnet default activation_threshold
pub const TESTNET_ACTIVATION_THRESHOLD: Ratio = Ratio::new(3, 4);
```
