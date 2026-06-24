Audit Report

## Title
Off-by-One Epoch Boundary in `rfc0044_active` Gate Skips `DaoScriptSizeVerifier` for First Block of Activation Epoch — (`verification/contextual/src/contextual_block_verifier.rs`)

## Summary
The gate enabling `DaoScriptSizeVerifier` passes `self.parent.epoch().number()` to `rfc0044_active` instead of the current block's epoch number. For the single first block of the RFC0044 activation epoch, the parent is still in epoch A-1, causing the gate to evaluate `false` and the verifier to be skipped entirely. A miner who wins that block slot can commit a DAO withdrawal whose output lock script byte-length differs from the deposit cell's lock script byte-length, permanently violating the RFC0044 invariant on-chain.

## Finding Description
At [1](#0-0)  the gate reads:

```rust
}.and_then(|result| {
    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
        DaoScriptSizeVerifier::new(...).verify()?;
    }
    Ok(result)
})
```

`rfc0044_active` is defined at [2](#0-1)  as:

```rust
pub fn rfc0044_active(&self, target: EpochNumber) -> bool {
    let rfc0044_active_epoch = match self.id.as_str() {
        mainnet::CHAIN_SPEC_NAME => softfork::mainnet::RFC0044_ACTIVE_EPOCH,  // 8651
        testnet::CHAIN_SPEC_NAME => softfork::testnet::RFC0044_ACTIVE_EPOCH,  // 5711
        _ => 0,
    };
    target >= rfc0044_active_epoch
}
```

For the first block of epoch A, `self.parent.epoch().number()` = A-1. The predicate becomes `(A-1) >= A` = `false`. `DaoScriptSizeVerifier` is not invoked. Every node running this binary evaluates the gate identically and accepts the block — there is no fork, but the RFC0044 lock-script-size invariant is permanently unenforceable for that one block. From epoch A+1 onward the gate correctly evaluates `true` (parent epoch = A ≥ A).

## Impact Explanation
This is an incorrect implementation of a system-level consensus rule (RFC0044 / DAO withdrawal invariant), matching the allowed impact: **"Incorrect implementation or behavior of CKB-VM or system scripts" — High (10001–15000 points)**. A miner can permanently commit a DAO withdrawal transaction that violates the lock-script-size invariant, corrupting chain state in a way that cannot be rolled back without a hard fork. The RFC0044 invariant exists to prevent capacity manipulation in DAO withdrawals; bypassing it for even one transaction undermines the economic guarantees of the DAO.

## Likelihood Explanation
The exploitable window is exactly one block per chain lifetime (the first block of the activation epoch). No special privilege beyond normal mining is required. The miner constructs the block template directly, bypassing the tx-pool's own `DaoScriptSizeVerifier` call. On mainnet the activation epoch is hardcoded at [3](#0-2)  (`RFC0044_ACTIVE_EPOCH = 8651`), and on testnet at [4](#0-3)  (`RFC0044_ACTIVE_EPOCH = 5711`). Any miner who wins that specific block slot — including a miner with a small hash-rate fraction — can exploit this.

## Recommendation
Replace `self.parent.epoch().number()` with the current block's own epoch number in the gate at [5](#0-4) :

```rust
if self.context.consensus.rfc0044_active(self.header.epoch().number()) {
```

This ensures `DaoScriptSizeVerifier` is applied to every block whose own epoch is ≥ `RFC0044_ACTIVE_EPOCH`, including the first block of the activation epoch.

## Proof of Concept
1. Configure a devnet with `RFC0044_ACTIVE_EPOCH = N` (small value for speed).
2. Mine until the chain tip is the last block of epoch N-1.
3. Construct a block at epoch N containing a DAO withdrawal transaction where the output lock script byte-length differs from the deposit cell's lock script byte-length.
4. Submit the block via RPC or P2P relay.
5. **Expected (buggy) result:** node accepts the block — gate evaluates `rfc0044_active(N-1)` = `false`, verifier skipped.
6. Repeat with a block at epoch N+1 (parent epoch = N): node rejects the same transaction — gate evaluates `rfc0044_active(N)` = `true`, verifier runs and returns an error.

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
