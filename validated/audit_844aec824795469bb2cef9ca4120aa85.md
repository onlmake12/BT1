Audit Report

## Title
Off-by-one epoch gate in `BlockTxsVerifier` silently skips `DaoScriptSizeVerifier` for the first block of RFC0044 activation epoch — (`verification/contextual/src/contextual_block_verifier.rs`)

## Summary

`BlockTxsVerifier::verify` gates `DaoScriptSizeVerifier` using `self.parent.epoch().number()` instead of `self.header.epoch().number()`. Because `rfc0044_active` uses a strict `>=` comparison, the first block of epoch `RFC0044_ACTIVE_EPOCH` is evaluated against epoch `RFC0044_ACTIVE_EPOCH - 1`, returning `false` and silently skipping the verifier for exactly that one block. A miner who produces that block can include a DAO withdrawal transaction where the withdrawal cell's lock script size differs from the deposit cell's, violating the RFC0044 invariant and enabling extraction of mismatched capacity from the DAO.

## Finding Description

`BlockTxsVerifier` (defined at `verification/contextual/src/contextual_block_verifier.rs:323`) holds both `header: HeaderView` (the current block) and `parent: &'b HeaderView` (the parent block). In `verify`, the gate at line 445 reads:

```rust
if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
    DaoScriptSizeVerifier::new(...).verify()?;
}
```

`rfc0044_active` (`spec/src/consensus.rs:1005`) is:

```rust
pub fn rfc0044_active(&self, target: EpochNumber) -> bool {
    ...
    target >= rfc0044_active_epoch
}
```

When the block being verified is the **first block of epoch `RFC0044_ACTIVE_EPOCH`**, its parent is the last block of epoch `RFC0044_ACTIVE_EPOCH - 1`. Passing `RFC0044_ACTIVE_EPOCH - 1` to `rfc0044_active` returns `false`, so `DaoScriptSizeVerifier` is never called. The verifier itself (`verification/src/transaction_verifier.rs:874`) has no epoch gate of its own — its only internal guard is a block-number check on the deposit cell's commit height. The sole epoch gate is the one in `BlockTxsVerifier`, and it is off by one.

`BlockExtensionVerifier` (`contextual_block_verifier.rs:540–543`) has the identical pattern, using `self.parent.epoch().number()` for its `rfc0044_active` check, meaning the first block of the activation epoch also skips MMR root verification.

## Impact Explanation

**Concrete impact: damages CKB economy (Critical, 15001–25000 points).**

RFC0044 enforces that a DAO withdrawal cell's lock script must be the same serialized size as the corresponding deposit cell's lock script. This prevents a depositor from substituting a smaller lock script at withdrawal time to free up capacity that was never theirs. By bypassing `DaoScriptSizeVerifier` for exactly one block, a miner can commit a DAO withdrawal where the withdrawal cell's lock script is smaller than the deposit cell's, extracting more free capacity than was deposited. This is a direct, concrete economic violation of the DAO accounting invariant.

## Likelihood Explanation

Any miner who wins the PoW lottery for the first block of epoch `RFC0044_ACTIVE_EPOCH` can exploit this. The activation epoch is publicly known and deterministic. The attacker pre-creates a DAO deposit cell with a known lock script, prepares a withdrawal transaction with a smaller lock script, and includes it in that one block. No special privilege beyond mining that block is required. The window is one block but is fully predictable and repeatable across testnets or devnets with configurable `RFC0044_ACTIVE_EPOCH`.

## Recommendation

Change the gate in `BlockTxsVerifier::verify` to use the current block's epoch number:

```rust
// Line 445: replace self.parent.epoch().number() with self.header.epoch().number()
if self.context.consensus.rfc0044_active(self.header.epoch().number()) {
    DaoScriptSizeVerifier::new(...).verify()?;
}
```

Apply the same fix to `BlockExtensionVerifier::verify` at line 543, replacing `self.parent.epoch().number()` with `block.header().epoch().number()` (since `BlockExtensionVerifier` receives the block as a parameter to `verify`).

## Proof of Concept

1. Configure a devnet with `RFC0044_ACTIVE_EPOCH = N`.
2. Create a DAO deposit cell with lock script of size `S`, committed at or after `starting_block_limiting_dao_withdrawing_lock`.
3. Construct a DAO withdrawal transaction spending that deposit cell, with a withdrawal cell whose lock script size is `S - 1` (smaller, freeing extra capacity).
4. Mine a block whose parent is the last block of epoch `N - 1` (i.e., the first block of epoch `N`) and include the transaction.
5. Run `BlockTxsVerifier::verify` — returns `Ok(...)` because `rfc0044_active(N - 1)` is `false`. Block is accepted.
6. Mine a block whose parent is the first block of epoch `N` (i.e., the second block of epoch `N`) and include the same transaction.
7. Run `BlockTxsVerifier::verify` — returns `Err(DaoLockSizeMismatch)` because `rfc0044_active(N)` is `true`. Block is rejected.

Step 5 demonstrates the bypass; step 7 confirms the fix works correctly one block later.