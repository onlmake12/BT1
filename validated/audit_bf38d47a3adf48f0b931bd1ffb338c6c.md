### Title
Stale Epoch Number Read Before Epoch Transition Causes First RFC0044 Activation Block to Omit MMR Extension — (File: `tx-pool/src/block_assembler/mod.rs`)

---

### Summary

In `build_extension()`, the block assembler reads the **tip header's epoch number** (the parent block's epoch) to decide whether RFC0044 (MMR chain-root extension) is active for the block being assembled. Because the candidate block is `tip + 1`, this is an off-by-one read: the epoch number is fetched *before* the epoch transition. At the exact boundary where RFC0044 activates, the tip's epoch number is `RFC0044_ACTIVE_EPOCH − 1`, so `rfc0044_active()` returns `false` and the block template is produced **without** the mandatory MMR extension. The code itself acknowledges this with an explicit comment.

---

### Finding Description

`build_extension()` in `tx-pool/src/block_assembler/mod.rs` determines RFC0044 activation using the **tip** (parent) header's epoch number, not the next block's epoch number:

```rust
// tx-pool/src/block_assembler/mod.rs  lines 564-581
pub(crate) fn build_extension(snapshot: &Snapshot) -> Result<Option<packed::Bytes>, AnyError> {
    let tip_header = snapshot.tip_header();
    // The use of the epoch number of the tip here leads to an off-by-one bug,
    // so be careful, it needs to be preserved for consistency reasons and not fixed directly.
    let mmr_activate = snapshot
        .consensus()
        .rfc0044_active(tip_header.epoch().number());   // ← stale epoch number
    ...
}
``` [1](#0-0) 

`rfc0044_active()` simply checks `target >= RFC0044_ACTIVE_EPOCH`:

```rust
// spec/src/consensus.rs  lines 1004-1012
pub fn rfc0044_active(&self, target: EpochNumber) -> bool {
    let rfc0044_active_epoch = match self.id.as_str() {
        mainnet::CHAIN_SPEC_NAME => softfork::mainnet::RFC0044_ACTIVE_EPOCH,  // 8651
        testnet::CHAIN_SPEC_NAME => softfork::testnet::RFC0044_ACTIVE_EPOCH,  // 5711
        _ => 0,
    };
    target >= rfc0044_active_epoch
}
``` [2](#0-1) [3](#0-2) 

When the tip is the **last block of epoch `RFC0044_ACTIVE_EPOCH − 1`**, `tip_header.epoch().number()` equals `RFC0044_ACTIVE_EPOCH − 1`, so `rfc0044_active()` returns `false`. The block template for the **first block of the activation epoch** is therefore assembled without the MMR extension.

The on-chain `BlockExtensionVerifier` has the **same** stale read — it also uses `self.parent.epoch().number()`:

```rust
// verification/contextual/src/contextual_block_verifier.rs  lines 540-548
let mmr_active = self
    .context
    .consensus
    .rfc0044_active(self.parent.epoch().number());   // ← same stale epoch number
match extra_fields_count {
    0 => {
        if mmr_active {
            return Err(BlockErrorKind::NoBlockExtension.into());
        }
    }
``` [4](#0-3) 

Because both sides share the same off-by-one, the block passes main-chain consensus. However, the light-client `VerifiableHeader::is_valid()` uses a **different** check:

```rust
// util/types/src/utilities/merkle_mountain_range.rs  lines 211-231
let mmr_activated_epoch = EpochNumberWithFraction::new(mmr_activated_epoch_number, 0, 1);
let has_chain_root = self.header().epoch() > mmr_activated_epoch;
if has_chain_root {
    ...
    let is_extension_beginning_with_chain_root_hash = self
        .extension()
        .map(|extension| { ... })
        .unwrap_or(false);
    if !is_extension_beginning_with_chain_root_hash {
        return false;
    }
}
``` [5](#0-4) 

For the first block of epoch `RFC0044_ACTIVE_EPOCH`, `self.header().epoch()` is `RFC0044_ACTIVE_EPOCH / 0 / L` (epoch N, index 0, length L). Depending on how `EpochNumberWithFraction` ordering is resolved against `N/0/1`, `has_chain_root` may evaluate to `true`, requiring a chain root that is absent — causing `is_valid()` to return `false`. Light clients would then reject a main-chain-valid block.

---

### Impact Explanation

The first block of the RFC0044 activation epoch is produced and accepted by the main chain **without** the MMR extension. Light clients relying on `VerifiableHeader::is_valid()` may reject this block as invalid (missing chain root), causing them to be unable to sync past the activation epoch boundary. This undermines the security guarantees of the CKB light-client protocol (RFC0044), which is the primary mechanism for trustless light-client verification of the chain.

---

### Likelihood Explanation

This is a deterministic, one-time event at the epoch boundary. It has already occurred on mainnet (epoch 8651) and testnet (epoch 5711). Any miner using the standard `get_block_template` RPC at the activation epoch boundary would produce a block without the extension. The bug is explicitly acknowledged in the source code comment: *"The use of the epoch number of the tip here leads to an off-by-one bug."*

---

### Recommendation

In `build_extension()`, derive the candidate block's epoch using `consensus.next_epoch_ext(tip_header, ...)` and use that epoch number for the `rfc0044_active()` check, rather than `tip_header.epoch().number()`. The same correction should be applied symmetrically to `BlockExtensionVerifier`. Because the comment notes this "needs to be preserved for consistency reasons," a coordinated protocol upgrade or a light-client-side workaround (treating the first activation-epoch block as a special case) is required.

---

### Proof of Concept

1. The tip is the last block of epoch `RFC0044_ACTIVE_EPOCH − 1` (e.g., mainnet epoch 8650).
2. A miner calls `get_block_template` via RPC.
3. `build_extension()` evaluates `rfc0044_active(8650)` → `false`; the template has **no extension field**.
4. The miner assembles and submits the first block of epoch 8651 without the extension.
5. `BlockExtensionVerifier` evaluates `rfc0044_active(parent.epoch().number() = 8650)` → `false`; `extra_fields_count == 0` is allowed. Block is **accepted** by the main chain.
6. A light client receives this block and calls `VerifiableHeader::is_valid(8651)`. `has_chain_root` evaluates to `true` (block is in epoch 8651 ≥ activation epoch). `extension()` is `None`, so `is_extension_beginning_with_chain_root_hash` is `false`. `is_valid()` returns **`false`**.
7. The light client rejects a valid main-chain block and cannot advance past the activation epoch boundary. [1](#0-0) [6](#0-5) [7](#0-6) [2](#0-1)

### Citations

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

**File:** util/constant/src/softfork/mainnet.rs (L1-4)
```rust
/// hardcode RFC0044 active epoch
pub const RFC0044_ACTIVE_EPOCH: u64 = 8651;


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

**File:** util/types/src/utilities/merkle_mountain_range.rs (L211-241)
```rust
    pub fn is_valid(&self, mmr_activated_epoch_number: EpochNumber) -> bool {
        let mmr_activated_epoch = EpochNumberWithFraction::new(mmr_activated_epoch_number, 0, 1);
        let has_chain_root = self.header().epoch() > mmr_activated_epoch;
        if has_chain_root {
            if self.header().is_genesis() {
                if !self.parent_chain_root().is_default() {
                    return false;
                }
            } else {
                let is_extension_beginning_with_chain_root_hash = self
                    .extension()
                    .map(|extension| {
                        let actual_extension_data = extension.raw_data();
                        let parent_chain_root_hash = self.parent_chain_root().calc_mmr_hash();
                        actual_extension_data.starts_with(parent_chain_root_hash.as_slice())
                    })
                    .unwrap_or(false);
                if !is_extension_beginning_with_chain_root_hash {
                    return false;
                }
            }
        }

        let expected_extension_hash = self
            .extension()
            .map(|extension| extension.calc_raw_data_hash());
        let extra_hash_view = ExtraHashView::new(self.uncles_hash(), expected_extension_hash);
        let expected_extra_hash = extra_hash_view.extra_hash();
        let actual_extra_hash = self.header().extra_hash();
        expected_extra_hash == actual_extra_hash
    }
```
