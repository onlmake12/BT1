### Title
Off-by-One Epoch Boundary in RFC0044 MMR Extension Activation Skips Chain-Root for First Activation-Epoch Block — (`tx-pool/src/block_assembler/mod.rs`, `verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

Both the block-template builder (`build_extension`) and the block verifier (`BlockExtensionVerifier::verify`) determine whether the RFC0044 MMR chain-root extension is required by querying `rfc0044_active` with the **parent** block's epoch number instead of the **candidate** block's epoch number. This off-by-one means the very first block of the activation epoch is assembled and accepted without the mandatory MMR extension, creating a one-block gap in chain-root coverage at the epoch boundary. The code itself explicitly documents this as a bug.

---

### Finding Description

`rfc0044_active` performs a simple threshold check:

```rust
// spec/src/consensus.rs:1005-1011
pub fn rfc0044_active(&self, target: EpochNumber) -> bool {
    let rfc0044_active_epoch = ...;  // 8651 mainnet, 5711 testnet
    target >= rfc0044_active_epoch
}
``` [1](#0-0) 

**In the block-template builder**, `build_extension` passes the *tip* (parent) epoch number:

```rust
// tx-pool/src/block_assembler/mod.rs:566-570
// The use of the epoch number of the tip here leads to an off-by-one bug,
// so be careful, it needs to be preserved for consistency reasons and not fixed directly.
let mmr_activate = snapshot
    .consensus()
    .rfc0044_active(tip_header.epoch().number());
``` [2](#0-1) 

**In the block verifier**, `BlockExtensionVerifier::verify` makes the identical mistake:

```rust
// verification/contextual/src/contextual_block_verifier.rs:540-543
let mmr_active = self
    .context
    .consensus
    .rfc0044_active(self.parent.epoch().number());
``` [3](#0-2) 

**The boundary scenario**: When the parent is the last block of epoch `RFC0044_ACTIVE_EPOCH − 1`, the candidate block is the first block of epoch `RFC0044_ACTIVE_EPOCH`. Both checks evaluate `rfc0044_active(RFC0044_ACTIVE_EPOCH − 1)` → `false`. Consequently:

1. `build_extension` returns `None` — no MMR extension is included in the block template.
2. The verifier's `mmr_active` is `false`, so it does **not** enforce `NoBlockExtension` and does **not** validate the MMR root even if an extension is present.

The correct argument should be the candidate block's epoch number (i.e., `RFC0044_ACTIVE_EPOCH`), which would make both checks return `true` and enforce the extension.

The hardcoded activation epochs are:

```rust
// util/constant/src/softfork/mainnet.rs
pub const RFC0044_ACTIVE_EPOCH: u64 = 8651;
// util/constant/src/softfork/testnet.rs
pub const RFC0044_ACTIVE_EPOCH: u64 = 5711;
``` [4](#0-3) [5](#0-4) 

---

### Impact Explanation

The first block of epoch `RFC0044_ACTIVE_EPOCH` is assembled by honest miners **without** the MMR chain-root extension and is accepted by all verifying nodes **without** the extension. This produces a one-block gap in MMR coverage at the activation boundary. Any light client or protocol component that expects the chain-root extension to be present in every block from the activation epoch onward will encounter an unexpected missing field for exactly this block. Additionally, because `mmr_active` is `false` for this block, a miner who *does* include an extension field has it accepted without MMR-root validation — meaning an invalid chain root could be committed to this block without rejection.

---

### Likelihood Explanation

The condition is triggered exactly once per chain: at the epoch boundary between `RFC0044_ACTIVE_EPOCH − 1` and `RFC0044_ACTIVE_EPOCH`. On mainnet this is epoch 8651; on testnet epoch 5711. The bug has already manifested on both networks. The code comment confirms developer awareness: *"The use of the epoch number of the tip here leads to an off-by-one bug, so be careful, it needs to be preserved for consistency reasons and not fixed directly."* Likelihood of future recurrence is zero for the same activation, but the pattern would repeat for any future buried softfork using the same `rfc0044_active`-style check applied to the parent epoch.

---

### Recommendation

The activation check should use the **candidate block's** epoch number, not the parent's:

```rust
// Correct: use the epoch of the block being built/verified
let candidate_epoch = snapshot.consensus()
    .next_epoch_ext(tip_header, &snapshot.borrow_as_data_loader())
    .map(|e| e.epoch().number())
    .unwrap_or(tip_header.epoch().number() + 1);
let mmr_activate = snapshot.consensus().rfc0044_active(candidate_epoch);
```

Because the chain has already been deployed with the off-by-one behavior, correcting it retroactively requires a hard fork or a new buried softfork that explicitly accounts for the one-block gap. Any future buried softfork activation check must use the candidate block's epoch, not the parent's.

---

### Proof of Concept

1. The chain reaches the last block of epoch `RFC0044_ACTIVE_EPOCH − 1` (e.g., epoch 8650 on mainnet). This block becomes the tip.
2. A miner calls `get_block_template` (RPC). `build_extension` is invoked with `tip_header = last block of epoch 8650`.
3. `rfc0044_active(8650)` → `8650 >= 8651` → `false`. `build_extension` returns `Ok(None)`.
4. The block template for the first block of epoch 8651 is assembled **without** the MMR extension.
5. The block is broadcast. `BlockExtensionVerifier::verify` is called with `parent = last block of epoch 8650`.
6. `rfc0044_active(8650)` → `false`. `extra_fields_count == 0` and `mmr_active == false` → `Ok(())`.
7. The first block of epoch 8651 is accepted by all nodes without the chain-root extension, skipping MMR coverage for this block — directly analogous to M-04's `tickTrackingIndex` not being incremented when `exitTimestamp == nextWeek`, causing the accrued state for that boundary period to be lost.

### Citations

**File:** spec/src/consensus.rs (L1004-1012)
```rust
    /// Returns whether rfc0044 is active  based on the epoch number
    pub fn rfc0044_active(&self, target: EpochNumber) -> bool {
        let rfc0044_active_epoch = match self.id.as_str() {
            mainnet::CHAIN_SPEC_NAME => softfork::mainnet::RFC0044_ACTIVE_EPOCH,
            testnet::CHAIN_SPEC_NAME => softfork::testnet::RFC0044_ACTIVE_EPOCH,
            _ => 0,
        };
        target >= rfc0044_active_epoch
    }
```

**File:** tx-pool/src/block_assembler/mod.rs (L564-581)
```rust
    pub(crate) fn build_extension(snapshot: &Snapshot) -> Result<Option<packed::Bytes>, AnyError> {
        let tip_header = snapshot.tip_header();
        // The use of the epoch number of the tip here leads to an off-by-one bug,
        // so be careful, it needs to be preserved for consistency reasons and not fixed directly.
        let mmr_activate = snapshot
            .consensus()
            .rfc0044_active(tip_header.epoch().number());
        if mmr_activate {
            let chain_root = snapshot
                .chain_root_mmr(tip_header.number())
                .get_root()
                .map_err(|e| InternalErrorKind::MMR.other(e))?;
            let bytes = chain_root.calc_mmr_hash().as_bytes().into();
            Ok(Some(bytes))
        } else {
            Ok(None)
        }
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L537-589)
```rust
    pub fn verify(&self, block: &BlockView) -> Result<(), Error> {
        let extra_fields_count = block.data().count_extra_fields();

        let mmr_active = self
            .context
            .consensus
            .rfc0044_active(self.parent.epoch().number());
        match extra_fields_count {
            0 => {
                if mmr_active {
                    return Err(BlockErrorKind::NoBlockExtension.into());
                }
            }
            1 => {
                let extension = if let Some(data) = block.extension() {
                    data
                } else {
                    return Err(BlockErrorKind::UnknownFields.into());
                };
                if extension.is_empty() {
                    return Err(BlockErrorKind::EmptyBlockExtension.into());
                }
                if extension.len() > 96 {
                    return Err(BlockErrorKind::ExceededMaximumBlockExtensionBytes.into());
                }
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
            }
            _ => {
                return Err(BlockErrorKind::UnknownFields.into());
            }
        }

        let actual_extra_hash = block.calc_extra_hash().extra_hash();
        if actual_extra_hash != block.extra_hash() {
            return Err(BlockErrorKind::InvalidExtraHash.into());
        }
        Ok(())
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
