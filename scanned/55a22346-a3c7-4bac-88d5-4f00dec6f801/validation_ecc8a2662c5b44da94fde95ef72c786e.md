### Title
Off-by-One in RFC0044 Activation Epoch Check Causes Missing MMR Extension at Epoch Boundary — (`tx-pool/src/block_assembler/mod.rs`)

---

### Summary

`build_extension` in the block assembler checks whether RFC0044 (MMR/light-client) is active using the **parent (tip) block's epoch number** rather than the **candidate block's epoch number**. The block verifier (`BlockExtensionVerifier`) makes the identical off-by-one check. As a result, the first block of the activation epoch is assembled and accepted without an MMR extension, creating a gap in the MMR chain that light clients cannot bridge through the chain-root proof mechanism.

---

### Finding Description

In `build_extension`, the activation gate reads:

```rust
// The use of the epoch number of the tip here leads to an off-by-one bug,
// so be careful, it needs to be preserved for consistency reasons and not fixed directly.
let mmr_activate = snapshot
    .consensus()
    .rfc0044_active(tip_header.epoch().number());   // ← parent epoch, not candidate epoch
``` [1](#0-0) 

`rfc0044_active` returns `true` only when `target >= RFC0044_ACTIVE_EPOCH`: [2](#0-1) 

`BlockExtensionVerifier::verify` applies the same check against the parent:

```rust
let mmr_active = self
    .context
    .consensus
    .rfc0044_active(self.parent.epoch().number());  // ← same off-by-one
``` [3](#0-2) 

**Scenario:** The parent block is the last block of epoch `RFC0044_ACTIVE_EPOCH − 1`. The candidate block is therefore the **first block of epoch `RFC0044_ACTIVE_EPOCH`**. Both the assembler and verifier evaluate `rfc0044_active(RFC0044_ACTIVE_EPOCH − 1)` → `false`. The candidate block is assembled and accepted **without** an MMR extension, even though it is the first block of the activation epoch.

The light-client validity check in `VerifiableHeader::is_valid` mirrors this gap:

```rust
let mmr_activated_epoch = EpochNumberWithFraction::new(mmr_activated_epoch_number, 0, 1);
let has_chain_root = self.header().epoch() > mmr_activated_epoch;
``` [4](#0-3) 

For the first block of the activation epoch (`index = 0, length = L`), the `Ord` comparison yields `0*1 == 0*L` → **Equal**, not Greater, so `has_chain_root = false`. The light client skips the chain-root proof check for exactly this block.

---

### Impact Explanation

The MMR chain is broken at the first block of the activation epoch. A light client receiving a `VerifiableHeader` for that block cannot verify MMR continuity; it falls back to PoW-only verification. A malicious light-client server can substitute a fake block at that position (with valid PoW) and the light client's chain-root check will not catch it. All subsequent blocks in the fake chain can carry self-consistent MMR extensions computed over the fake first block, making the forgery undetectable through the MMR proof path alone.

---

### Likelihood Explanation

The window is narrow (one block per chain, at a fixed epoch boundary — mainnet epoch 8651, testnet epoch 5711). Exploitation requires the attacker to mine a block with valid PoW at that boundary and operate a malicious light-client server. The code comment acknowledges the bug explicitly and notes it is preserved for consistency, meaning it has not been corrected and the gap persists on both networks. [5](#0-4) 

---

### Recommendation

In `build_extension`, derive the candidate block's epoch from `consensus.next_epoch_ext(tip_header, ...)` and use that epoch number for the activation check, rather than `tip_header.epoch().number()`. Apply the same correction symmetrically in `BlockExtensionVerifier::verify` (use the candidate block's epoch, not the parent's). Update `VerifiableHeader::is_valid` to use `>=` instead of `>` so that the first block of the activation epoch is also required to carry a chain root.

---

### Proof of Concept

1. Let `A = RFC0044_ACTIVE_EPOCH` (mainnet: 8651).
2. Parent block P is the last block of epoch `A − 1` (`index == length − 1`).
3. `build_extension` calls `rfc0044_active(A − 1)` → `false` → no extension in candidate block C (first block of epoch A).
4. `BlockExtensionVerifier` calls `rfc0044_active(A − 1)` → `false` → accepts C without extension. C is committed to the chain.
5. A malicious light-client server replaces C with C′ (same epoch position, different transactions, valid PoW).
6. Light client calls `is_valid` on C′: `epoch(C′) == mmr_activated_epoch` → `has_chain_root = false` → no chain-root check performed. C′ is accepted.
7. For block C′+1 onward, the server provides self-consistent MMR extensions rooted at C′, completing the fork undetected by the MMR proof path. [6](#0-5) [7](#0-6)

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

**File:** verification/contextual/src/contextual_block_verifier.rs (L540-543)
```rust
        let mmr_active = self
            .context
            .consensus
            .rfc0044_active(self.parent.epoch().number());
```

**File:** util/types/src/utilities/merkle_mountain_range.rs (L211-213)
```rust
    pub fn is_valid(&self, mmr_activated_epoch_number: EpochNumber) -> bool {
        let mmr_activated_epoch = EpochNumberWithFraction::new(mmr_activated_epoch_number, 0, 1);
        let has_chain_root = self.header().epoch() > mmr_activated_epoch;
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
