### Title
Off-by-One in RFC0044 Epoch Activation Check Causes MMR Extension to Be Skipped for the Entire First Activation Epoch — (`tx-pool/src/block_assembler/mod.rs` and `verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

Both the block assembler (`build_extension`) and the consensus verifier (`BlockExtensionVerifier::verify`) use the **parent block's** epoch number — not the candidate block's epoch number — when checking whether RFC0044 (the MMR chain-root extension for light clients) is active. This off-by-one causes the MMR extension to be neither produced nor enforced for every block in the entire first epoch after the configured `RFC0044_ACTIVE_EPOCH` constant. The code itself explicitly acknowledges this as a bug but preserves it for on-chain consistency.

---

### Finding Description

**Block assembler side** — `tx-pool/src/block_assembler/mod.rs`, `build_extension`:

```rust
pub(crate) fn build_extension(snapshot: &Snapshot) -> Result<Option<packed::Bytes>, AnyError> {
    let tip_header = snapshot.tip_header();
    // The use of the epoch number of the tip here leads to an off-by-one bug,
    // so be careful, it needs to be preserved for consistency reasons and not fixed directly.
    let mmr_activate = snapshot
        .consensus()
        .rfc0044_active(tip_header.epoch().number());   // ← parent epoch, not candidate epoch
``` [1](#0-0) 

**Consensus verifier side** — `verification/contextual/src/contextual_block_verifier.rs`, `BlockExtensionVerifier::verify`:

```rust
let mmr_active = self
    .context
    .consensus
    .rfc0044_active(self.parent.epoch().number());   // ← parent epoch, not block's own epoch
``` [2](#0-1) 

The activation predicate is:

```rust
pub fn rfc0044_active(&self, target: EpochNumber) -> bool {
    ...
    target >= rfc0044_active_epoch   // mainnet: 8651, testnet: 5711
}
``` [3](#0-2) 

**Root cause — epoch arithmetic:**

When building or verifying block `N+1`, the parent is block `N`. If block `N` is the **last block of epoch E−1**, then `parent.epoch().number() = E−1`. The check `E−1 >= RFC0044_ACTIVE_EPOCH` (where `RFC0044_ACTIVE_EPOCH = E`) evaluates to **false**, so the extension is neither generated nor required. The correct check should use the candidate block's own epoch number (which would be `E`), not the parent's.

Concretely:

| Block position | Parent epoch | `rfc0044_active(parent_epoch)` | Extension required? |
|---|---|---|---|
| First block of epoch E (activation epoch) | E−1 | false | **No** (off-by-one) |
| First block of epoch E+1 | E | true | Yes |

Every block in epoch E — the entire first activation epoch — is produced and accepted **without** the MMR chain-root extension, even though the protocol intends it to be mandatory from epoch E onward. [4](#0-3) [5](#0-4) 

---

### Impact Explanation

RFC0044 introduces a Merkle Mountain Range (MMR) chain-root commitment in the block extension field so that light clients can efficiently verify the chain without downloading all headers. The off-by-one means:

1. **All blocks in epoch E (the configured activation epoch) lack the MMR extension.** The verifier accepts them without it.
2. **Light clients cannot obtain or verify MMR proofs for any block in epoch E.** A malicious full node can serve those blocks to a light client without the chain-root commitment, and the light client has no cryptographic anchor to detect equivocation or a forged chain segment covering that epoch.
3. **The activation boundary is silently shifted by one full epoch** (≈4 hours on mainnet with a 1743-block epoch at ~8 s/block). Any security guarantee that RFC0044 was supposed to provide from epoch E is absent for that entire epoch.

The impact class is **consensus validation / block extension accounting**: the enforcement of a mandatory field is incorrectly gated, analogous to the external report's numerator being off by a factor of 10 — the accounting invariant is wrong by exactly one unit of the relevant scale (one epoch instead of one decimal place). [6](#0-5) 

---

### Likelihood Explanation

**High.** This is a deterministic, unconditional code path executed for every block produced and verified. It requires no special attacker capability — any miner building a block at the epoch-E boundary will naturally omit the extension (because `build_extension` returns `None`), and the verifier will accept it. The behavior is already live on mainnet (epoch 8651) and testnet (epoch 5711). The code comment confirms the bug is real and was knowingly preserved.

---

### Recommendation

The correct fix is to use the **candidate block's** epoch number (i.e., the epoch of the block being built or verified, not its parent) in both `build_extension` and `BlockExtensionVerifier::verify`. Because the bug is already deployed on mainnet, a direct fix would cause a consensus split for historical blocks. The recommended approach is to introduce a second hard-fork activation constant (e.g., `RFC0044_CORRECT_ACTIVE_EPOCH = RFC0044_ACTIVE_EPOCH + 1`) that reflects the actual on-chain activation boundary, and use the candidate block's epoch for all future checks, while documenting the one-epoch gap in the protocol specification.

---

### Proof of Concept

Let `RFC0044_ACTIVE_EPOCH = E` (e.g., 8651 on mainnet).

1. The last block of epoch E−1 is mined. Its epoch field encodes epoch number E−1.
2. A miner calls `get_block_template` RPC. `build_extension` is invoked with `tip_header` = last block of epoch E−1.
3. `tip_header.epoch().number()` = E−1. `rfc0044_active(E−1)` = `E−1 >= E` = **false**.
4. `build_extension` returns `Ok(None)` — no extension is included in the block template.
5. The miner submits the block (first block of epoch E) without an extension.
6. `BlockExtensionVerifier::verify` is called with `self.parent` = last block of epoch E−1.
7. `self.parent.epoch().number()` = E−1. `rfc0044_active(E−1)` = **false**.
8. `extra_fields_count == 0` and `mmr_active == false` → **no error**, block accepted.
9. This repeats for every block in epoch E (all have a parent whose epoch number is E−1 or E but the first block of epoch E+1 is the first to have a parent with epoch number E).
10. A light client requesting an MMR proof for any block in epoch E receives no chain-root commitment and cannot verify the block's inclusion in the canonical chain. [7](#0-6) [8](#0-7)

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
