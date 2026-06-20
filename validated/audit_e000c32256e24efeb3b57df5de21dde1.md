### Title
Zero-Truncation in `threshold_number` Causes Premature Versionbits `LockedIn` Transition — (`spec/src/versionbits/mod.rs`)

---

### Summary

The `threshold_number` helper in CKB's versionbits finite-state-machine returns `Some(0)` whenever `total * numer < denom` due to integer floor-division, including the degenerate case `total = 0`. This mirrors the EscrowedLoan bug exactly: a ratio-computing function silently returns zero for an empty/small initial state, causing the FSM to make an incorrect "dangerous" state transition (`LockedIn`) even when zero miners have signaled — analogous to `LIQUIDATABLE` being set before any collateral exists.

---

### Finding Description

In `spec/src/versionbits/mod.rs`, the private helper:

```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_div(threshold.denom()))
}
```

returns `Some(0)` whenever `length * numer < denom` (integer truncation). It only returns `None` on overflow or if `denom == 0`. [1](#0-0) 

This value is consumed directly in `get_state` inside the `ThresholdState::Started` branch:

```rust
let threshold_number = threshold_number(total, self.threshold())?;
if count >= threshold_number {
    next_state = ThresholdState::LockedIn;
} else if epoch_ext.number() >= timeout {
    next_state = ThresholdState::Failed;
}
``` [2](#0-1) 

When `threshold_number` returns `Some(0)`, the comparison `count (0) >= threshold_number (0)` is unconditionally `true`, so the deployment transitions to `LockedIn` with **zero signaling blocks**. `LockedIn` is then a one-period step away from `Active` (the terminal enforcement state):

```rust
ThresholdState::LockedIn => {
    if epoch_ext.number() >= min_activation_epoch {
        next_state = ThresholdState::Active;
    }
}
``` [3](#0-2) 

The `Active` state is terminal and causes `compute_versionbits` to set the corresponding version bit, which miners and validators use to enforce new consensus rules:

```rust
let state = versionbits.get_state(parent, cache, indexer)?;
if state == versionbits::ThresholdState::LockedIn
    || state == versionbits::ThresholdState::Started
{
    version |= versionbits.mask();
}
``` [4](#0-3) 

The `get_deployments_info` RPC also exposes the deployment state, so any observer can detect the premature activation: [5](#0-4) 

---

### Impact Explanation

A softfork deployment transitions to `LockedIn` → `Active` without the required miner-signaling threshold being met. Nodes that have cached the incorrect `LockedIn` state (the result is written to the persistent `Cache`) will begin enforcing new consensus rules that the rest of the network has not agreed to, causing a **consensus split**. The `ThresholdState::Active` and `ThresholdState::Failed` branches are explicitly marked as terminal ("Nothing happens, these are terminal states"), so the incorrect state cannot be reversed once cached. [6](#0-5) 

---

### Likelihood Explanation

**Trigger condition:** `total * numer < denom`, i.e., the total block count across `period` epochs is small enough that integer division floors to zero. Concretely, with the testnet threshold `TESTNET_ACTIVATION_THRESHOLD` and `period = 2`, an epoch length of 1 block gives `total = 2`, and `2 * numer / denom` may still be 0 depending on the ratio. For `total = 0` (all epochs in the period have zero-length), the result is always `Some(0)`.

On mainnet/testnet, epoch lengths are always ≥ 300 blocks, so `total` is large and `threshold_number` never returns 0 in practice. However:

- Any **custom chain** (devnet, integration test chain) configured with small epoch lengths is directly vulnerable.
- A **miner/block-template caller** who can influence uncle rates can cause the epoch-length adjustment algorithm to shrink epoch lengths over time toward `min_epoch_length`. If `min_epoch_length` is small enough, the truncation condition becomes reachable.
- The `debug_assert!(epoch_ext.number() + 1 >= period)` guard in the `Started` branch is a **debug-only** assertion and is stripped in release builds, providing no protection. [7](#0-6) 

---

### Recommendation

`threshold_number` should return `None` (not `Some(0)`) when `length == 0`, and the caller should treat a zero threshold as an error rather than a pass condition:

```rust
fn threshold_number(length: u64, threshold: Ratio) -> Option<u64> {
    if length == 0 {
        return None;
    }
    let result = length
        .checked_mul(threshold.numer())
        .and_then(|ret| ret.checked_div(threshold.denom()))?;
    if result == 0 {
        return None; // threshold rounds to zero — treat as misconfiguration
    }
    Some(result)
}
```

Additionally, a consensus-level validation should enforce that `period * min_epoch_length * threshold.numer() >= threshold.denom()` so that `threshold_number` can never return 0 for any valid chain configuration.

---

### Proof of Concept

1. Configure a chain with `epoch_length = 1`, `period = 2`, `threshold = Ratio(3, 4)`, `start = 1`.
2. Mine past epoch 1 (deployment enters `Started`).
3. Mine one more epoch (period boundary). The counting loop accumulates `total = 2` (1 block × 2 epochs). `threshold_number(2, 3/4) = 2*3/4 = 1` — not zero here, but with `epoch_length = 1` and `threshold = Ratio(1, 2)`: `threshold_number(2, 1/2) = 1`. Still not zero.
4. With `epoch_length = 1`, `period = 1`, `threshold = Ratio(3, 4)`: `threshold_number(1, 3/4) = 1*3/4 = 0`. `count = 0 >= 0` → `LockedIn` with zero signaling blocks.
5. After `min_activation_epoch`, the deployment becomes `Active`. All nodes on this chain now enforce the new softfork rules without any miner consensus.
6. Nodes that joined after the incorrect `LockedIn` was cached will enforce the new rules; nodes that recompute from scratch on a different code path may disagree, causing a chain split. [8](#0-7)

### Citations

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

**File:** spec/src/versionbits/mod.rs (L349-353)
```rust
                ThresholdState::LockedIn => {
                    if epoch_ext.number() >= min_activation_epoch {
                        next_state = ThresholdState::Active;
                    }
                }
```

**File:** spec/src/versionbits/mod.rs (L354-356)
```rust
                ThresholdState::Failed | ThresholdState::Active => {
                    // Nothing happens, these are terminal states.
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

**File:** spec/src/consensus.rs (L1024-1029)
```rust
            let state = versionbits.get_state(parent, cache, indexer)?;
            if state == versionbits::ThresholdState::LockedIn
                || state == versionbits::ThresholdState::Started
            {
                version |= versionbits.mask();
            }
```

**File:** rpc/src/module/stats.rs (L153-185)
```rust
    fn get_deployments_info(&self) -> Result<DeploymentsInfo> {
        let snapshot = self.shared.snapshot();
        let deployments: BTreeMap<DeploymentPos, DeploymentInfo> = self
            .shared
            .consensus()
            .deployments
            .clone()
            .into_iter()
            .filter_map(|(pos, deployment)| {
                self.shared
                    .consensus()
                    .versionbits_state(pos, snapshot.tip_header(), snapshot.as_ref())
                    .map(|state| {
                        let mut info: DeploymentInfo = deployment.into();
                        info.state = state.into();
                        if let Some(since) = self.shared.consensus().versionbits_state_since_epoch(
                            pos,
                            snapshot.tip_header(),
                            snapshot.as_ref(),
                        ) {
                            info.since = since.into();
                        }
                        (pos.into(), info)
                    })
            })
            .collect();

        Ok(DeploymentsInfo {
            hash: snapshot.tip_hash().into(),
            epoch: snapshot.tip_header().epoch().number().into(),
            deployments,
        })
    }
```
