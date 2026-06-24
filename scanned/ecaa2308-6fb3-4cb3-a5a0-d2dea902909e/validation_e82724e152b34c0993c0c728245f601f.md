The code at line 445 is confirmed to use `self.parent.epoch().number()`: [1](#0-0) 

`BlockTxsVerifier` holds both `header` (current block) and `parent` (parent block) as distinct fields: [2](#0-1) 

The tx-pool applies `DaoScriptSizeVerifier` unconditionally, confirming the bypass requires `submit_block`: [3](#0-2) 

---

Audit Report

## Title
`DaoScriptSizeVerifier` Skipped for First Block of RFC0044 Activation Epoch Due to Off-by-One Epoch Parameter — (`File: verification/contextual/src/contextual_block_verifier.rs`)

## Summary
In `BlockTxsVerifier::verify`, the hardfork activation guard for `DaoScriptSizeVerifier` passes `self.parent.epoch().number()` to `rfc0044_active` instead of `self.header.epoch().number()`. At the exact epoch boundary where RFC0044 activates, this causes `DaoScriptSizeVerifier` to be silently skipped for every non-cellbase transaction in the first block of the activation epoch. A miner can exploit this by manually assembling a block containing a DAO output script that exceeds the RFC0044 size limit and submitting it via `submit_block` RPC, causing a consensus split between nodes running the buggy code and nodes running a corrected build.

## Finding Description
`BlockTxsVerifier` stores both `header` (the current block's header) and `parent` (the parent block's header) as distinct fields. In `verify`, after each transaction is verified, the code conditionally applies `DaoScriptSizeVerifier`:

```rust
// Line 444-453
.and_then(|result| {
    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
        DaoScriptSizeVerifier::new(...).verify()?;
    }
    Ok(result)
})
```

The guard uses `self.parent.epoch().number()` — the epoch of the **parent** block — rather than `self.header.epoch().number()`. For the first block of RFC0044 activation epoch `N`, the parent is the last block of epoch `N-1`. Therefore `rfc0044_active(N-1)` returns `false` and `DaoScriptSizeVerifier` is never called for any transaction in that block. For all subsequent blocks in epoch `N`, the parent is also in epoch `N`, so the check runs correctly. The bug is confined to exactly one block per activation epoch.

The tx-pool applies `DaoScriptSizeVerifier` unconditionally (no `rfc0044_active` guard), so a crafted oversized-DAO transaction cannot enter the pool via `send_transaction`. The attacker must bypass the tx-pool entirely by assembling the block manually and submitting it via the `submit_block` RPC.

## Impact Explanation
This is a **Critical** vulnerability matching the impact class: *"Vulnerabilities which could easily cause consensus deviation."* Nodes running the buggy code accept the first block of epoch `N` containing an oversized DAO script. Any node running a corrected build evaluates `rfc0044_active(N)` = `true`, runs `DaoScriptSizeVerifier`, and rejects the block. This produces a deterministic, permanent chain split at the activation epoch boundary.

## Likelihood Explanation
The attack requires a malicious miner who knows the RFC0044 activation epoch (deterministic from the chain spec), can craft a transaction with an oversized DAO output script, and can submit a manually assembled block via the standard `submit_block` RPC. No special privileges beyond operating a mining node are required. The window is exactly one block, but it is fully predictable and the attack can be prepared in advance. Likelihood is low in practice but the preconditions are realistic for a motivated miner.

## Recommendation
Replace `self.parent.epoch().number()` with `self.header.epoch().number()` in the `rfc0044_active` guard:

```rust
if self.context.consensus.rfc0044_active(self.header.epoch().number()) {
```

Add a unit test that verifies `DaoScriptSizeVerifier` is enforced specifically for the **first** block of the RFC0044 activation epoch (where the parent is in epoch `N-1`).

## Proof of Concept
1. Determine RFC0044 activation epoch `N` from the chain spec.
2. Craft transaction `T` with a DAO-type output script exceeding the RFC0044 size limit. Submitting via `send_transaction` RPC will be rejected by the tx-pool's unconditional `DaoScriptSizeVerifier`.
3. Identify block height `H` = first block of epoch `N` (parent is last block of epoch `N-1`).
4. Manually assemble a block at height `H` including `T` directly in the transaction list, bypassing the tx-pool.
5. Submit via `submit_block` RPC.
6. The buggy node accepts the block: `rfc0044_active(self.parent.epoch().number())` = `rfc0044_active(N-1)` = `false`, so `DaoScriptSizeVerifier` is never invoked.
7. A corrected node evaluates `rfc0044_active(self.header.epoch().number())` = `rfc0044_active(N)` = `true`, runs `DaoScriptSizeVerifier`, and rejects the block — producing a consensus split.

### Citations

**File:** verification/contextual/src/contextual_block_verifier.rs (L323-329)
```rust
struct BlockTxsVerifier<'a, 'b, CS> {
    context: VerifyContext<CS>,
    header: HeaderView,
    handle: &'a Handle,
    txs_verify_cache: &'a Arc<RwLock<TxVerificationCache>>,
    parent: &'b HeaderView,
}
```

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

**File:** tx-pool/src/util.rs (L117-131)
```rust
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
```
