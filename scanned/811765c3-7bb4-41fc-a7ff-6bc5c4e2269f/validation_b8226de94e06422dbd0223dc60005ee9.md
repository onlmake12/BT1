### Title
Off-by-one in RFC0044 MMR extension epoch-boundary check causes first activation-epoch block to lack chain-root commitment — (`tx-pool/src/block_assembler/mod.rs`)

---

### Summary

`build_extension` in the block assembler checks whether RFC0044 (MMR light-client support) is active by querying the **tip/parent** block's epoch number instead of the **candidate block's** epoch number. The same off-by-one is mirrored in `BlockExtensionVerifier`. The two sides agree with each other, so the network accepts the resulting block, but the first block of the RFC0044 activation epoch is permanently assembled and committed on-chain **without** an MMR chain-root extension. A malicious sync peer can therefore serve a forged copy of that specific block to any RFC0044 light client without detection.

---

### Finding Description

`build_extension` decides whether to embed the MMR chain-root hash in the candidate block's extension field:

```rust
// tx-pool/src/block_assembler/mod.rs  line 564-570
pub(crate) fn build_extension(snapshot: &Snapshot) -> Result<Option<packed::Bytes>, AnyError> {
    let tip_header = snapshot.tip_header();
    // The use of the epoch number of the tip here leads to an off-by-one bug,
    // so be careful, it needs to be preserved for consistency reasons and not fixed directly.
    let mmr_activate = snapshot
        .consensus()
        .rfc0044_active(tip_header.epoch().number());   // ← parent epoch, not candidate epoch
``` [1](#0-0) 

`rfc0044_active` simply tests `target >= RFC0044_ACTIVE_EPOCH`: [2](#0-1) 

`BlockExtensionVerifier::verify` carries the identical off-by-one on the validation side:

```rust
// verification/contextual/src/contextual_block_verifier.rs  line 540-543
let mmr_active = self
    .context
    .consensus
    .rfc0044_active(self.parent.epoch().number());   // ← parent epoch, not block's own epoch
``` [3](#0-2) 

Because both sides use the **parent's** epoch number, they agree: the first block of epoch `RFC0044_ACTIVE_EPOCH` (mainnet: 8651, testnet: 5711) is built and accepted **without** an MMR extension. The developer comment in the assembler explicitly acknowledges this as a bug that is "preserved for consistency reasons." [4](#0-3) [5](#0-4) 

The light-client verification path (`VerifiableHeader::is_valid`) uses a strict `>` comparison:

```rust
let mmr_activated_epoch = EpochNumberWithFraction::new(mmr_activated_epoch_number, 0, 1);
let has_chain_root = self.header().epoch() > mmr_activated_epoch;
``` [6](#0-5) 

For the first block of epoch N (fraction `(N, 0, L)`), `(N, 0, L) > (N, 0, 1)` evaluates to **false** (both fractions equal zero), so `has_chain_root = false` and no chain-root check is performed. This is consistent with the missing extension, but it means the light-client protocol provides **zero MMR protection** for that block.

---

### Impact Explanation

**Impact: High.**

RFC0044 light clients rely on the MMR chain-root commitment embedded in each block's extension field to verify that a header served by a full node is part of the canonical chain. Because the first block of the activation epoch carries no chain-root commitment and `VerifiableHeader::is_valid` skips the chain-root check for it, a malicious sync peer can substitute an entirely fabricated block at that position. The light client will accept the fake block as valid (extra-hash check passes because there is no extension to hash), and the fraud is undetectable for that single block. Any state derived from that block (e.g., transaction inclusion proofs anchored to it) is therefore unverifiable.

---

### Likelihood Explanation

**Likelihood: Medium.**

The event is deterministic and guaranteed to occur exactly once per chain at the RFC0044 activation epoch boundary (already reached on testnet at epoch 5711; scheduled for mainnet at epoch 8651). Any light client that syncs across that boundary is exposed. A malicious full node that is aware of the gap can exploit it without any special capability — it only needs to be a peer of the target light client.

---

### Recommendation

The correct fix requires a coordinated change to **both** the assembler and the verifier so that the candidate block's own epoch number is used:

**In `build_extension`** (`tx-pool/src/block_assembler/mod.rs`): derive the candidate epoch from `current_epoch.number()` (already computed in the calling context) rather than from `tip_header.epoch().number()`.

**In `BlockExtensionVerifier::verify`** (`verification/contextual/src/contextual_block_verifier.rs`): use `block.epoch().number()` (the block being verified) instead of `self.parent.epoch().number()`.

**In `VerifiableHeader::is_valid`** (`util/types/src/utilities/merkle_mountain_range.rs`): change the boundary condition from strict `>` to `>=` so that the first block of the activation epoch is also required to carry a chain-root commitment.

All three changes must be deployed together as a consensus-breaking upgrade to avoid a split between old and new nodes.

---

### Proof of Concept

1. Let `A = RFC0044_ACTIVE_EPOCH` (8651 on mainnet).
2. The last block of epoch `A-1` is the tip. `tip_header.epoch().number() = A-1`.
3. `rfc0044_active(A-1)` → `A-1 >= A` → **false** → `build_extension` returns `None`.
4. The candidate block (first block of epoch `A`) is assembled **without** an extension field.
5. `BlockExtensionVerifier` checks `rfc0044_active(parent.epoch().number())` = `rfc0044_active(A-1)` = **false** → no extension required → block accepted.
6. A light client calls `VerifiableHeader::is_valid` on this block: `epoch() = (A, 0, L)`, `mmr_activated_epoch = (A, 0, 1)`, `(A,0,L) > (A,0,1)` → **false** → `has_chain_root = false` → no chain-root check performed.
7. A malicious peer replaces the block body with arbitrary data. The light client accepts it because the only check performed is the extra-hash, which passes trivially when there is no extension. [7](#0-6) [8](#0-7) [9](#0-8)

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

**File:** util/types/src/utilities/merkle_mountain_range.rs (L210-241)
```rust
    /// Checks if the current verifiable header is valid.
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
