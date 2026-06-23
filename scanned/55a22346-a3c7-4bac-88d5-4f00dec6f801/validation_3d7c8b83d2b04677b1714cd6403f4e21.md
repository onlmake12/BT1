### Title
Off-by-One in RFC0044 Epoch Boundary Check Causes First Activation-Epoch Block to Omit MMR Extension — (`File: tx-pool/src/block_assembler/mod.rs` and `verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

`build_extension` in the block assembler and `BlockExtensionVerifier::verify` in the consensus verifier both evaluate RFC0044 (MMR chain-root extension) activation using the **parent/tip block's epoch number** instead of the **candidate block's epoch number**. This is an acknowledged off-by-one that causes the first block of the RFC0044 activation epoch to be assembled and accepted by full nodes **without** an MMR extension field, while the light-client validity check (`VerifiableHeader::is_valid`) uses a stricter boundary and **expects** a chain root for that same block. The inconsistency is self-consistent between assembler and full-node verifier (both share the same off-by-one), but breaks light-client header verification at the activation boundary.

---

### Finding Description

**Block assembler (`build_extension`):**

```rust
// tx-pool/src/block_assembler/mod.rs  line 564-581
pub(crate) fn build_extension(snapshot: &Snapshot) -> Result<Option<packed::Bytes>, AnyError> {
    let tip_header = snapshot.tip_header();
    // The use of the epoch number of the tip here leads to an off-by-one bug,
    // so be careful, it needs to be preserved for consistency reasons and not fixed directly.
    let mmr_activate = snapshot
        .consensus()
        .rfc0044_active(tip_header.epoch().number());   // ← tip epoch, not candidate epoch
    ...
}
``` [1](#0-0) 

The candidate block being assembled is `tip + 1`. When the tip is the last block of epoch 8650 (mainnet) or 5710 (testnet), the candidate block is the **first block of the activation epoch** (8651 / 5711). But `rfc0044_active(8650)` returns `false`, so no extension is included.

**Consensus verifier (`BlockExtensionVerifier::verify`):**

```rust
// verification/contextual/src/contextual_block_verifier.rs  line 540-543
let mmr_active = self
    .context
    .consensus
    .rfc0044_active(self.parent.epoch().number());   // ← parent epoch, same off-by-one
``` [2](#0-1) 

Because the verifier also uses the parent's epoch number, it accepts the extension-less block. Both sides share the same off-by-one, so the full-node chain is internally consistent.

**Activation function:**

```rust
// spec/src/consensus.rs  line 1005-1011
pub fn rfc0044_active(&self, target: EpochNumber) -> bool {
    let rfc0044_active_epoch = match self.id.as_str() {
        mainnet::CHAIN_SPEC_NAME => softfork::mainnet::RFC0044_ACTIVE_EPOCH,  // 8651
        testnet::CHAIN_SPEC_NAME => softfork::testnet::RFC0044_ACTIVE_EPOCH,  // 5711
        _ => 0,
    };
    target >= rfc0044_active_epoch
}
``` [3](#0-2) [4](#0-3) 

**Light-client verifier (`VerifiableHeader::is_valid`) uses a different, stricter boundary:**

```rust
// util/types/src/utilities/merkle_mountain_range.rs  line 211-213
pub fn is_valid(&self, mmr_activated_epoch_number: EpochNumber) -> bool {
    let mmr_activated_epoch = EpochNumberWithFraction::new(mmr_activated_epoch_number, 0, 1);
    let has_chain_root = self.header().epoch() > mmr_activated_epoch;
``` [5](#0-4) 

For the first block of epoch 8651, `header.epoch()` encodes as `EpochNumberWithFraction(8651, 0, ~1800)`. Its full-value integer is `(8651 << 54) | (0 << 24) | 1800`, which is strictly greater than `EpochNumberWithFraction(8651, 0, 1)` = `(8651 << 54) | (0 << 24) | 1`. So `has_chain_root = true`, and `is_valid` expects a chain-root hash in the extension — but the block has none. `is_valid` returns `false`.

---

### Impact Explanation

Any light-client peer that calls `VerifiableHeader::is_valid` to verify the first block of the RFC0044 activation epoch will **incorrectly reject a canonically valid block**. This breaks light-client sync at the activation boundary: the light client cannot advance past that block, effectively stalling header verification for the entire chain beyond that point. The impact is bounded to one block height but is permanent — the block is already on-chain and cannot be changed.

**Impact: 3 / 5** — Disrupts light-client header verification at a specific, fixed block; full nodes are unaffected.

---

### Likelihood Explanation

The activation epoch boundary is a single, deterministic event. On mainnet (epoch 8651) and testnet (epoch 5711) the boundary has already passed, meaning the extension-less block is already committed to the canonical chain. Any light client syncing through that block height will encounter the failure. The trigger requires no attacker — it is reached by any light-client sync peer traversing the canonical chain.

**Likelihood: 3 / 5** — Certain to be encountered by any light client syncing past the activation epoch; no adversarial action required.

---

### Recommendation

The off-by-one is explicitly preserved in the codebase comment ("needs to be preserved for consistency reasons and not fixed directly") because changing the full-node consensus rule now would cause a chain split. The correct remediation is in `VerifiableHeader::is_valid`: align its boundary check with the deployed consensus rule by using `>=` on the epoch number rather than `>` on the epoch fraction, so that the first block of the activation epoch is not required to carry a chain root:

```rust
// Align with deployed consensus: parent epoch >= activation epoch triggers MMR
let has_chain_root = self.header().epoch().number() > mmr_activated_epoch_number;
```

This makes the light-client check consistent with what `BlockExtensionVerifier` actually enforces on-chain, without touching consensus-critical code.

---

### Proof of Concept

1. Obtain the first block of mainnet epoch 8651 (the RFC0044 activation epoch). Its `extension` field is absent or does not contain a 32-byte chain-root hash.
2. Construct a `VerifiableHeader` from that block header with `extension = None`.
3. Call `verifiable_header.is_valid(8651)`.
4. Observe: `mmr_activated_epoch = EpochNumberWithFraction::new(8651, 0, 1)`. The block's epoch fraction `(8651, 0, ~1800)` compares greater, so `has_chain_root = true`. Because `extension` is `None`, `is_extension_beginning_with_chain_root_hash` evaluates to `false` via `.unwrap_or(false)`, and `is_valid` returns `false`.
5. Meanwhile, `BlockExtensionVerifier::verify` on the same block calls `rfc0044_active(parent.epoch().number())` = `rfc0044_active(8650)` = `false`, so it accepts the block with no extension — confirming the divergence between full-node consensus and light-client validation. [6](#0-5) [7](#0-6)

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

**File:** verification/contextual/src/contextual_block_verifier.rs (L540-543)
```rust
        let mmr_active = self
            .context
            .consensus
            .rfc0044_active(self.parent.epoch().number());
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L544-548)
```rust
        match extra_fields_count {
            0 => {
                if mmr_active {
                    return Err(BlockErrorKind::NoBlockExtension.into());
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

**File:** util/constant/src/softfork/mainnet.rs (L1-2)
```rust
/// hardcode RFC0044 active epoch
pub const RFC0044_ACTIVE_EPOCH: u64 = 8651;
```

**File:** util/types/src/utilities/merkle_mountain_range.rs (L211-213)
```rust
    pub fn is_valid(&self, mmr_activated_epoch_number: EpochNumber) -> bool {
        let mmr_activated_epoch = EpochNumberWithFraction::new(mmr_activated_epoch_number, 0, 1);
        let has_chain_root = self.header().epoch() > mmr_activated_epoch;
```

**File:** util/types/src/utilities/merkle_mountain_range.rs (L220-231)
```rust
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
```
