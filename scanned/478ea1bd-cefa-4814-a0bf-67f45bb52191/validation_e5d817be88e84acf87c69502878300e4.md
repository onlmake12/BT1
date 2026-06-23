### Title
Softfork Activation Threshold Enforced Below Configured Ratio Due to Floor Division in `threshold_number` - (File: `spec/src/versionbits/mod.rs`)

### Summary

The `threshold_number` function in `spec/src/versionbits/mod.rs` uses integer floor division to compute the minimum number of signaling blocks required to lock in a softfork. Because Rust's integer division truncates toward zero, the computed threshold can be strictly less than `total * (numer/denom)`, allowing a softfork to activate with fewer miner signals than the configured ratio requires. This is the direct CKB analog of the Governor.sol boundary-condition bug: a comparison that should enforce a strict minimum ratio instead permits activation at a lower-than-configured threshold.

### Finding Description

The softfork activation logic in `VersionbitsConditionChecker::get_state` counts how many blocks in the signaling period set the deployment bit, then calls `threshold_number` to determine the minimum required count:

```rust
// spec/src/versionbits/mod.rs:342-344
let threshold_number = threshold_number(total, self.threshold())?;
if count >= threshold_number {
    next_state = ThresholdState::LockedIn;
}
```

`threshold_number` is defined as:

```rust
// spec/src/versionbits/mod.rs:475-479
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_div(threshold.denom()))
}
```

This computes `floor(length × numer / denom)`. When `length × numer` is not evenly divisible by `denom`, the result is strictly less than the true rational value `length × numer / denom`. The condition `count >= threshold_number` therefore passes with a count that is below the configured threshold ratio.

**Concrete example — testnet threshold `TESTNET_ACTIVATION_THRESHOLD = Ratio::new(3, 4)` (75%):**

| `total` blocks | `threshold_number` (floor) | Actual ratio enforced | Configured ratio |
|---|---|---|---|
| 10 | `floor(10×3/4) = 7` | 7/10 = **70%** | 75% |
| 14 | `floor(14×3/4) = 10` | 10/14 = **71.4%** | 75% |
| 6  | `floor(6×3/4) = 4`  | 4/6 = **66.7%** | 75% |

**Concrete example — mainnet threshold `LC_MAINNET_ACTIVATION_THRESHOLD = Ratio::new(8, 10)` (80%):**

| `total` blocks | `threshold_number` (floor) | Actual ratio enforced | Configured ratio |
|---|---|---|---|
| 3 | `floor(3×8/10) = 2` | 2/3 = **66.7%** | 80% |
| 7 | `floor(7×8/10) = 5` | 5/7 = **71.4%** | 80% |

Because CKB epoch lengths are variable (they adjust dynamically based on actual block times), `total` — the sum of block counts across `period` epochs — will frequently not be divisible by the threshold denominator, making this boundary condition regularly triggered in practice.

The correct computation requires ceiling division: `ceil(length × numer / denom) = (length × numer + denom − 1) / denom`, which guarantees the enforced count is never below the configured ratio.

### Impact Explanation

A miner coalition controlling slightly fewer blocks than the configured threshold fraction can successfully signal a softfork into `LockedIn` state, and subsequently `Active` state. This means consensus rule changes (e.g., the Light Client protocol deployment `DeploymentPos::LightClient`) can be activated without the level of miner consensus the protocol parameters require. Once `Active`, the softfork is irreversible on that chain. Nodes that have not upgraded may be split from the network by a softfork that was activated without the intended supermajority of miner support.

### Likelihood Explanation

The bug is triggered whenever `total × numer` is not divisible by `denom`. Since CKB epoch lengths vary dynamically, this is a common condition. The attacker needs to control close to (but slightly below) the configured threshold fraction of hash power — not a 51% attack, but a coalition near the threshold. For the testnet threshold of 75%, a coalition with as few as ~67–70% of blocks in a period can trigger lock-in depending on the epoch length. The entry path is fully unprivileged: any miner or block-template caller can set the signaling bit in the cellbase witness version field, as checked by `condition` in `Versionbits`.

### Recommendation

Replace floor division with ceiling division in `threshold_number`:

```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .map(|product| {
            // ceiling division: (product + denom - 1) / denom
            (product + threshold.denom() - 1) / threshold.denom()
        })
}
```

This ensures that `count >= threshold_number` is only satisfied when the true ratio `count / total >= numer / denom`.

### Proof of Concept

With `TESTNET_ACTIVATION_THRESHOLD = Ratio::new(3, 4)` and an epoch of length 5 (so `period = 2` gives `total = 10`):

- Current code: `threshold_number(10, 3/4) = floor(30/4) = 7`
- A miner coalition signaling in 7 out of 10 blocks (70%) satisfies `count >= 7` → `LockedIn`
- But 7/10 = 70% < 75% = the configured threshold

The `condition` function reads the cellbase witness version bits set by miners:

```rust
// spec/src/versionbits/mod.rs:440-454
fn condition<I: VersionbitsIndexer>(&self, header: &HeaderView, indexer: &I) -> bool {
    if let Some(cellbase) = indexer.cellbase(&header.hash())
        && let Some(witness) = cellbase.witnesses().get(0)
        && let Ok(reader) = CellbaseWitnessReader::from_slice(&witness.raw_data())
    {
        let message = reader.message().to_entity();
        if message.len() >= 4
            && let Ok(raw) = message.raw_data()[..4].try_into()
        {
            let version = u32::from_le_bytes(raw);
            return ((version & VERSIONBITS_TOP_MASK) == VERSIONBITS_TOP_BITS)
                && (version & self.mask()) != 0;
        }
    }
    false
}
```

Any miner producing blocks can set this bit. The `get_state` function then counts these signals and compares against the under-computed `threshold_number`, locking in the softfork below the intended threshold. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** spec/src/versionbits/mod.rs (L138-140)
```rust
    /// Specifies the minimum ratio of block per `period`,
    /// which indicate the locked_in of the softfork during the `period`.
    pub threshold: Ratio,
```

**File:** spec/src/versionbits/mod.rs (L342-344)
```rust
                    let threshold_number = threshold_number(total, self.threshold())?;
                    if count >= threshold_number {
                        next_state = ThresholdState::LockedIn;
```

**File:** spec/src/versionbits/mod.rs (L440-454)
```rust
    fn condition<I: VersionbitsIndexer>(&self, header: &HeaderView, indexer: &I) -> bool {
        if let Some(cellbase) = indexer.cellbase(&header.hash())
            && let Some(witness) = cellbase.witnesses().get(0)
            && let Ok(reader) = CellbaseWitnessReader::from_slice(&witness.raw_data())
        {
            let message = reader.message().to_entity();
            if message.len() >= 4
                && let Ok(raw) = message.raw_data()[..4].try_into()
            {
                let version = u32::from_le_bytes(raw);
                return ((version & VERSIONBITS_TOP_MASK) == VERSIONBITS_TOP_BITS)
                    && (version & self.mask()) != 0;
            }
        }
        false
```

**File:** spec/src/versionbits/mod.rs (L475-479)
```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_div(threshold.denom()))
}
```

**File:** spec/src/consensus.rs (L99-101)
```rust
pub const LC_MAINNET_ACTIVATION_THRESHOLD: Ratio = Ratio::new(8, 10);
/// The testnet default activation_threshold
pub const TESTNET_ACTIVATION_THRESHOLD: Ratio = Ratio::new(3, 4);
```
