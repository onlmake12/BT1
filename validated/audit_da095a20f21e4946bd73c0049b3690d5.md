### Title
Off-by-One Epoch Boundary in `rfc0044_active` Gate Allows `DaoScriptSizeVerifier` Bypass for First Block of Activation Epoch — (`verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

The gate that enables `DaoScriptSizeVerifier` uses the **parent block's** epoch number rather than the **current block's** epoch number. For the single block that is the first block of the RFC0044 activation epoch, the parent is still in epoch N-1, so the gate evaluates to `false` and the verifier is skipped entirely. A miner who wins that block can include a DAO withdrawal whose output lock script differs in size from the deposit cell's lock script, violating the RFC0044 invariant.

---

### Finding Description

The gate is at: [1](#0-0) 

```rust
}.and_then(|result| {
    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
        DaoScriptSizeVerifier::new(...).verify()?;
    }
    Ok(result)
})
```

`rfc0044_active` is: [2](#0-1) 

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

Let `A` = `RFC0044_ACTIVE_EPOCH`. For the **first block of epoch A**, `self.parent.epoch().number()` = `A - 1`. The check becomes `A - 1 >= A` = `false`. `DaoScriptSizeVerifier` is not called. The block is accepted by every node running this code.

---

### Impact Explanation

A miner who produces the first block of epoch A can include a DAO withdrawal transaction whose output lock script has a different byte-length than the deposit cell's lock script. This transaction passes all block-level validation (the size check is the only guard for this invariant). The transaction is permanently committed to the canonical chain, violating the RFC0044 invariant for that withdrawal.

The "consensus deviation between nodes" framing in the question is **incorrect**: all nodes running the same binary evaluate the gate identically and all accept the block. There is no fork. The actual harm is a one-block window where the RFC0044 lock-script-size invariant is unenforceable.

---

### Likelihood Explanation

The window is exactly one block per chain lifetime (the first block of epoch A). Any miner who wins that block slot can exploit it. No special privilege is required beyond normal mining. The miner constructs the block template directly, bypassing the tx-pool's own `DaoScriptSizeVerifier` call entirely.

---

### Recommendation

Replace `self.parent.epoch().number()` with the **current block's** epoch number in the gate:

```rust
if self.context.consensus.rfc0044_active(self.header.epoch().number()) {
```

This ensures the verifier is applied to every block whose own epoch is ≥ `RFC0044_ACTIVE_EPOCH`, including the first block of the activation epoch.

---

### Proof of Concept

1. Configure a devnet with `RFC0044_ACTIVE_EPOCH = N`.
2. Mine blocks until the chain tip is the last block of epoch N-1 (parent epoch = N-1).
3. Construct a block at epoch N containing a DAO withdrawal transaction where the output lock script byte-length differs from the deposit cell's lock script byte-length.
4. Submit the block via P2P relay or RPC.
5. Observe: the node accepts the block (gate evaluates `rfc0044_active(N-1)` = false, verifier skipped).
6. Repeat with a block at epoch N+1 (parent epoch = N): the node rejects the same transaction (gate evaluates `rfc0044_active(N)` = true, verifier runs and fails).

This confirms the one-block bypass window at the epoch boundary.

### Citations

**File:** verification/contextual/src/contextual_block_verifier.rs (L444-453)
```rust
                }.and_then(|result| {
                    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
                        DaoScriptSizeVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                        ).verify()?;
                    }
                    Ok(result)
                })
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
