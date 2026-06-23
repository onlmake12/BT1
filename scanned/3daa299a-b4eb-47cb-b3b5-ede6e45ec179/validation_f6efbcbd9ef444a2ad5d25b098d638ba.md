### Title
Off-by-One in RFC0044 MMR Activation Epoch Check Causes Missing Chain Root in First Block of Activation Epoch — (`tx-pool/src/block_assembler/mod.rs` and `verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

An off-by-one error in the epoch number used to gate RFC0044 (MMR/light-client) activation causes the first block of the activation epoch to be assembled without a chain root extension and accepted by the verifier without chain root validation. This is structurally identical to the UlyssesToken bug: an index is read from the wrong object (parent/tip instead of the candidate block), producing a boundary inconsistency that silently breaks a protocol invariant.

---

### Finding Description

**Root cause — block assembler (`build_extension`):**

```rust
// tx-pool/src/block_assembler/mod.rs  L564-580
pub(crate) fn build_extension(snapshot: &Snapshot) -> Result<Option<packed::Bytes>, AnyError> {
    let tip_header = snapshot.tip_header();
    // The use of the epoch number of the tip here leads to an off-by-one bug,
    // so be careful, it needs to be preserved for consistency reasons and not fixed directly.
    let mmr_activate = snapshot
        .consensus()
        .rfc0044_active(tip_header.epoch().number());   // ← uses TIP epoch, not CANDIDATE epoch
    if mmr_activate {
        ...chain root included...
    } else {
        Ok(None)   // ← no chain root for first block of activation epoch
    }
}
``` [1](#0-0) 

The block being assembled is at `tip_number + 1`. Its epoch is determined by `next_epoch_ext(tip_header, …)`. When `tip_header` is the **last block of epoch N−1**, the candidate block is the **first block of epoch N**. If `RFC0044_ACTIVE_EPOCH = N`, then:

- `tip_header.epoch().number()` = N−1  
- `rfc0044_active(N−1)` = **false**  
- The chain root extension is **omitted** from the first block of epoch N.

**Root cause — block verifier (`BlockExtensionVerifier`):**

```rust
// verification/contextual/src/contextual_block_verifier.rs  L540-543
let mmr_active = self
    .context
    .consensus
    .rfc0044_active(self.parent.epoch().number());  // ← same off-by-one
``` [2](#0-1) 

Because the verifier uses the same wrong epoch number, it sets `mmr_active = false` for the first block of the activation epoch. The consequence is twofold:

1. A block **without** an extension is accepted (no `NoBlockExtension` error).  
2. A block **with** an extension is accepted **without** chain root validation (the `if mmr_active { … }` guard is skipped). [3](#0-2) 

The activation epoch constants are hardcoded:

- Mainnet: epoch **8651** (`RFC0044_ACTIVE_EPOCH`)  
- Testnet: epoch **5711** [4](#0-3) [5](#0-4) 

The `rfc0044_active` check:

```rust
pub fn rfc0044_active(&self, target: EpochNumber) -> bool {
    target >= rfc0044_active_epoch   // strict ≥, so epoch N-1 fails when active epoch is N
}
``` [6](#0-5) 

---

### Impact Explanation

**Consequence 1 — Missing chain root (deterministic).**  
The first block of the RFC0044 activation epoch is always assembled and accepted without a chain root in its extension. Light clients relying on RFC0044 cannot verify the MMR chain root for this block, breaking the continuity of the MMR proof chain at the exact activation boundary.

**Consequence 2 — Unvalidated extension data (exploitable by any miner).**  
Because `mmr_active = false` for this block, the verifier skips chain root validation even when an extension *is* present. A miner who wins the first block of the activation epoch can include **arbitrary bytes** in the extension field. The full node verifier accepts the block. Light clients that parse the extension as a chain root will receive a forged value, corrupting their view of the MMR state from the activation point onward.

This is the direct CKB analog of the UlyssesToken accounting error: just as removing an asset assigned the wrong (0-based) ID to the swapped-in asset — silently corrupting the ID map — here the wrong (parent) epoch number is used to gate the chain root, silently omitting or leaving unvalidated the MMR commitment at the critical boundary block.

---

### Likelihood Explanation

- The bug is **deterministic**: it fires for exactly one block per deployment (the first block of the activation epoch). On mainnet that block has already been produced (epoch 8651); on any future network deployment it will fire again at the configured activation epoch.  
- Any miner — no special privilege beyond winning the PoW lottery for that one block — can include a forged chain root that passes full-node verification.  
- The code comment explicitly acknowledges the bug: *"The use of the epoch number of the tip here leads to an off-by-one bug, so be careful, it needs to be preserved for consistency reasons and not fixed directly."* This confirms the root cause is known and the vulnerable code path is live.

---

### Recommendation

The correct fix requires a coordinated change to both sites so that the **candidate block's epoch number** is used, not the parent's:

**Assembler fix:**
```rust
// Compute the candidate block's epoch before checking activation
let candidate_epoch = snapshot
    .consensus()
    .next_epoch_ext(tip_header, &snapshot.borrow_as_data_loader())
    .map(|e| e.epoch().number())
    .unwrap_or_else(|| tip_header.epoch().number());
let mmr_activate = snapshot.consensus().rfc0044_active(candidate_epoch);
```

**Verifier fix:**
```rust
// Use the epoch of the block being verified, not its parent
let mmr_active = self
    .context
    .consensus
    .rfc0044_active(block.header().epoch().number());
```

Both changes must be deployed atomically (e.g., via a hardfork switch) to avoid a consensus split.

---

### Proof of Concept

Given `ProposalWindow(2, 10)` and `RFC0044_ACTIVE_EPOCH = N`:

1. Mine blocks up to the last block of epoch N−1 (the tail block). Its `epoch().number()` = N−1.  
2. Call `build_extension` for the next block (first of epoch N):  
   - `tip_header.epoch().number()` = N−1  
   - `rfc0044_active(N−1)` = false → extension omitted.  
3. Submit the block. `BlockExtensionVerifier` checks `rfc0044_active(parent.epoch().number())` = `rfc0044_active(N−1)` = false → no chain root required → block accepted.  
4. Alternatively, submit the block with a 32-byte all-zeros extension. The verifier skips chain root validation (same false guard) → block accepted with forged chain root.  
5. A light client syncing from this block receives either no chain root or a forged one, breaking RFC0044 proof continuity.

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

**File:** verification/contextual/src/contextual_block_verifier.rs (L544-577)
```rust
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
