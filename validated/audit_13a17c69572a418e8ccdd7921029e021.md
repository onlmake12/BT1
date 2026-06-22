### Title
RFC0044 MMR Extension Epoch-Boundary Desync Between Block Assembler and Activation Specification - (File: `tx-pool/src/block_assembler/mod.rs`)

### Summary
`build_extension` checks RFC0044 activation using the **tip (parent) block's epoch number** rather than the new block's epoch number. At the exact epoch boundary where RFC0044 activates, the first block of the activation epoch is assembled without the mandatory MMR chain-root extension. The `BlockExtensionVerifier` uses the same off-by-one reference, so full nodes accept the block, but the desync between the intended activation epoch and the actual activation epoch creates a verifiable gap for light clients.

### Finding Description
In `build_extension`, the activation gate is:

```rust
let mmr_activate = snapshot
    .consensus()
    .rfc0044_active(tip_header.epoch().number());  // uses PARENT epoch, not new block's epoch
```

The code comment explicitly acknowledges this:

> "The use of the epoch number of the tip here leads to an off-by-one bug, so be careful, it needs to be preserved for consistency reasons and not fixed directly."

`rfc0044_active` is a simple threshold check:

```rust
pub fn rfc0044_active(&self, target: EpochNumber) -> bool {
    target >= rfc0044_active_epoch   // mainnet: 8651, testnet: 5711
}
```

When the new block is the **first block of epoch 8651**, the parent's epoch number is **8650**, so `rfc0044_active(8650)` = `false`. The block assembler omits the MMR extension. The `BlockExtensionVerifier` applies the identical check:

```rust
let mmr_active = self
    .context
    .consensus
    .rfc0044_active(self.parent.epoch().number());  // same off-by-one
```

So full nodes accept the extension-less block. The desync is between:
- **What RFC0044 specifies**: extension mandatory from epoch 8651 onwards
- **What the implementation delivers**: extension mandatory from the *second* block of epoch 8651 onwards

This is the direct CKB analog of the external report's pattern: two coupled parameters (the activation gate and the extension content) are evaluated against different epoch reference points at the state-transition boundary.

### Impact Explanation
Light clients using the RFC0044 light-client protocol rely on the MMR chain-root hash embedded in the block extension to verify the chain without downloading all blocks. The first block of the activation epoch carries no extension, creating a proof gap. A malicious full node serving this block to a light client can exploit the inconsistency: the light client cannot construct or verify an MMR proof for this block, breaking the chain of trust at the activation boundary. Light-client implementations that strictly follow the RFC (extension required from epoch 8651) will reject or fail to verify this block, while implementations that follow the actual node behavior will silently accept a block with no MMR commitment.

### Likelihood Explanation
This is a deterministic, protocol-level desync that fires exactly once per activation epoch boundary. On mainnet it affects epoch 8651; on testnet epoch 5711. No attacker action is required — any miner producing the first block of the activation epoch will produce an extension-less block, and any full node serving it to a light client exposes the inconsistency. The entry path is reachable by any light-client peer connected to a full node.

### Recommendation
The activation check in `build_extension` should use the **new block's epoch number** (available from `current_epoch` already computed in `update_blank`/`update_full`) rather than the tip's epoch number. The same correction must be applied consistently to `BlockExtensionVerifier` to avoid a new consensus split. Because the comment warns that fixing this in isolation breaks consistency, both sites must be updated atomically and the change must be treated as a consensus-breaking protocol upgrade.

### Proof of Concept
1. Chain reaches the last block of epoch 8650 (mainnet). Tip epoch = 8650.
2. Block assembler calls `build_extension(snapshot)`:
   - `tip_header.epoch().number()` = 8650
   - `rfc0044_active(8650)` = `8650 >= 8651` = **false**
   - Extension is **not** included in the block template.
3. Miner produces and broadcasts the first block of epoch 8651 — no MMR extension field.
4. `BlockExtensionVerifier::verify` on receiving nodes:
   - `self.parent.epoch().number()` = 8650
   - `rfc0044_active(8650)` = **false**
   - `extra_fields_count == 0` branch: no error. Block **accepted**.
5. A light client connecting to this full node requests a proof for epoch 8651 blocks. The first block has no chain-root hash; the light client cannot verify its MMR commitment and either rejects the block or accepts it without the expected proof, breaking the RFC0044 security guarantee at the activation boundary. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** verification/contextual/src/contextual_block_verifier.rs (L537-577)
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

**File:** util/constant/src/softfork/testnet.rs (L1-2)
```rust
/// hardcode RFC0044 active epoch
pub const RFC0044_ACTIVE_EPOCH: u64 = 5711;
```
