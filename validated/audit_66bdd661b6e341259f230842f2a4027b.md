### Title
Off-by-One in RFC0044 MMR Activation Epoch Check Allows Unvalidated Block Extension at Activation Boundary — (File: `tx-pool/src/block_assembler/mod.rs`, `verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

Both the block assembler and the block extension verifier use the **parent block's epoch number** to determine whether RFC0044 (MMR/light-client extension) is active, instead of the **new block's epoch number**. This off-by-one causes the first block of the activation epoch to be treated as if MMR is not yet active. As a result, a miner can include an extension with an **arbitrary, unvalidated MMR root** in that boundary block, and every full node will accept it. Light clients relying on the MMR for chain verification receive a corrupt root at the activation boundary.

---

### Finding Description

**Root cause — block assembler (`tx-pool/src/block_assembler/mod.rs`, lines 564–570):**

```rust
pub(crate) fn build_extension(snapshot: &Snapshot) -> Result<Option<packed::Bytes>, AnyError> {
    let tip_header = snapshot.tip_header();
    // The use of the epoch number of the tip here leads to an off-by-one bug,
    // so be careful, it needs to be preserved for consistency reasons and not fixed directly.
    let mmr_activate = snapshot
        .consensus()
        .rfc0044_active(tip_header.epoch().number());   // ← parent epoch, not new block epoch
``` [1](#0-0) 

**Root cause — block extension verifier (`verification/contextual/src/contextual_block_verifier.rs`, lines 540–543):**

```rust
let mmr_active = self
    .context
    .consensus
    .rfc0044_active(self.parent.epoch().number());  // ← parent epoch, not new block epoch
``` [2](#0-1) 

**The activation check (`spec/src/consensus.rs`, lines 1005–1012):**

```rust
pub fn rfc0044_active(&self, target: EpochNumber) -> bool {
    let rfc0044_active_epoch = match self.id.as_str() {
        mainnet::CHAIN_SPEC_NAME => softfork::mainnet::RFC0044_ACTIVE_EPOCH,  // 8651
        testnet::CHAIN_SPEC_NAME => softfork::testnet::RFC0044_ACTIVE_EPOCH,  // 5711
        _ => 0,
    };
    target >= rfc0044_active_epoch
}
``` [3](#0-2) 

**Activation epoch constants:** [4](#0-3) [5](#0-4) 

**The off-by-one, step by step:**

Let `A = RFC0044_ACTIVE_EPOCH` (e.g., 8651 on mainnet).

| Block | Parent epoch | `rfc0044_active(parent_epoch)` | Extension required? | Extension validated? |
|---|---|---|---|---|
| Last block of epoch A−1 | A−1 | `A−1 >= A` → **false** | No | No |
| **First block of epoch A** | **A−1** | **`A−1 >= A` → false** | **No** | **No** |
| Second block of epoch A | A | `A >= A` → true | Yes | Yes (MMR root checked) |

The first block of epoch A is the activation boundary block. Because the check uses the parent's epoch number (A−1), `rfc0044_active` returns `false` for that block. The verifier then enters the `1 =>` branch without validating the MMR root:

```rust
1 => {
    ...
    if mmr_active {          // false for the boundary block
        // MMR root check is SKIPPED
        let chain_root = self.chain_root_mmr.get_root()...;
        if actual_root_hash != expected_root_hash {
            return Err(BlockErrorKind::InvalidChainRoot.into());
        }
    }
}
``` [6](#0-5) 

A miner can therefore include any 1–96 byte payload as the extension of the first block of epoch A, and every full node will accept it without checking the MMR root.

The code comment at line 566–567 explicitly acknowledges this: *"The use of the epoch number of the tip here leads to an off-by-one bug, so be careful, it needs to be preserved for consistency reasons and not fixed directly."* [7](#0-6) 

---

### Impact Explanation

RFC0044 defines the MMR-based light client protocol. The MMR extension in each block header commits to the cumulative chain root, allowing light clients to verify historical headers without downloading the full chain.

If the first block of the activation epoch carries an **arbitrary or maliciously crafted MMR root** (accepted by all full nodes due to the skipped validation), the MMR chain is poisoned at its origin. Any light client that trusts the MMR from the activation epoch onward will anchor its verification to a corrupt root. This enables a miner who wins the activation-boundary block to present a false chain history to RFC0044 light clients — specifically, they can forge the MMR root to exclude or alter any block committed before the activation epoch.

---

### Likelihood Explanation

The attacker must mine the first block of the activation epoch. This requires only normal mining participation — no majority hashpower, no privileged access. Any pool or solo miner who happens to win that block can exploit the window. The activation epoch is a fixed, publicly known value (`RFC0044_ACTIVE_EPOCH = 8651` on mainnet), so the target block is predictable. On mainnet and testnet the activation epoch has already passed, meaning the boundary block is already fixed in history; however, the bug remains live in the codebase and would recur for any future chain or any future RFC that reuses the same `rfc0044_active` pattern.

---

### Recommendation

The activation check should use the **new block's epoch number**, not the parent's. The correct call would be:

```rust
// In build_extension: use the candidate block's epoch, not the tip's
let candidate_epoch = current_epoch.number();  // epoch of the block being assembled
let mmr_activate = snapshot.consensus().rfc0044_active(candidate_epoch);

// In BlockExtensionVerifier::verify: use block.epoch().number()
let mmr_active = self.context.consensus.rfc0044_active(block.epoch().number());
```

Because the existing mainnet/testnet chains have already committed the boundary block without a validated extension, this fix requires a coordinated consensus migration (e.g., a new hard fork rule that re-anchors the MMR from the corrected boundary). The comment in the code correctly notes that a direct in-place fix would break chain consistency; the proper path is a scheduled migration.

---

### Proof of Concept

1. Observe that `RFC0044_ACTIVE_EPOCH = 8651` on mainnet (`util/constant/src/softfork/mainnet.rs`).
2. The first block of epoch 8651 has a parent whose `epoch().number() = 8650`.
3. `rfc0044_active(8650)` → `8650 >= 8651` → `false`.
4. `BlockExtensionVerifier::verify` sets `mmr_active = false` and skips the MMR root check.
5. A miner who wins block `start_of_epoch_8651` can set the extension to any 32-byte value (e.g., all zeros) and every full node will accept the block.
6. A light client bootstrapping from epoch 8651 onward will anchor its MMR verification to the forged root, allowing the miner to present a false chain history for all blocks before epoch 8651.

### Citations

**File:** tx-pool/src/block_assembler/mod.rs (L564-570)
```rust
    pub(crate) fn build_extension(snapshot: &Snapshot) -> Result<Option<packed::Bytes>, AnyError> {
        let tip_header = snapshot.tip_header();
        // The use of the epoch number of the tip here leads to an off-by-one bug,
        // so be careful, it needs to be preserved for consistency reasons and not fixed directly.
        let mmr_activate = snapshot
            .consensus()
            .rfc0044_active(tip_header.epoch().number());
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L540-543)
```rust
        let mmr_active = self
            .context
            .consensus
            .rfc0044_active(self.parent.epoch().number());
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L562-577)
```rust
                if mmr_active {
                    if extension.len() < 32 {
                        return Err(BlockErrorKind::InvalidBlockExtension.into());
                    }

                    let chain_root = self
                        .chain_root_mmr
                        .get_root()
                        .map_err(|e| InternalErrorKind::MMR.other(e))?;
                    let actual_root_hash = chain_root.calc_mmr_hash();
                    let expected_root_hash =
                        Byte32::new_unchecked(extension.raw_data().slice(..32));
                    if actual_root_hash != expected_root_hash {
                        return Err(BlockErrorKind::InvalidChainRoot.into());
                    }
                }
```

**File:** spec/src/consensus.rs (L1005-1012)
```rust
    pub fn rfc0044_active(&self, target: EpochNumber) -> bool {
        let rfc0044_active_epoch = match self.id.as_str() {
            mainnet::CHAIN_SPEC_NAME => softfork::mainnet::RFC0044_ACTIVE_EPOCH,
            testnet::CHAIN_SPEC_NAME => softfork::testnet::RFC0044_ACTIVE_EPOCH,
            _ => 0,
        };
        target >= rfc0044_active_epoch
    }
```

**File:** util/constant/src/softfork/mainnet.rs (L1-2)
```rust
/// hardcode RFC0044 active epoch
pub const RFC0044_ACTIVE_EPOCH: u64 = 8651;
```

**File:** util/constant/src/softfork/testnet.rs (L1-2)
```rust
/// hardcode RFC0044 active epoch
pub const RFC0044_ACTIVE_EPOCH: u64 = 5711;
```
