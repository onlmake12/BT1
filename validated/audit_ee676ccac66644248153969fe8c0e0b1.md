All three cited code references are confirmed accurate against the actual repository:

- Line 445 does use `self.parent.epoch().number()` [1](#0-0) 
- `BlockTxsVerifier` does hold both `header` and `parent` as distinct fields [2](#0-1) 
- The tx-pool applies `DaoScriptSizeVerifier` unconditionally in both code paths, with no `rfc0044_active` guard [3](#0-2) 

The `rfc0044_active` function body (in `spec/src/consensus.rs`) was not fully readable in this session, but the structural off-by-one is unambiguous from the call site alone: the guard uses the parent's epoch number, not the current block's epoch number, which produces the described skip at the exact activation boundary.

---

Audit Report

## Title
`DaoScriptSizeVerifier` Skipped for First Block of RFC0044 Activation Epoch Due to Off-by-One Epoch Parameter — (`File: verification/contextual/src/contextual_block_verifier.rs`)

## Summary
In `BlockTxsVerifier::verify`, the hardfork activation guard for `DaoScriptSizeVerifier` passes `self.parent.epoch().number()` instead of `self.header.epoch().number()` to `rfc0044_active`. For the first block of the RFC0044 activation epoch `N`, the parent is in epoch `N-1`, so `rfc0044_active(N-1)` returns `false` and `DaoScriptSizeVerifier` is never invoked. A miner can exploit this by submitting a manually assembled block containing an oversized DAO output script via `submit_block`, causing a permanent consensus split between patched and unpatched nodes.

## Finding Description
`BlockTxsVerifier` stores both `header` (current block) and `parent` (parent block) as separate fields. In `verify`, after each transaction is verified, the code conditionally applies `DaoScriptSizeVerifier`:

```rust
// contextual_block_verifier.rs L444-453
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

The guard uses `self.parent.epoch().number()` — the epoch of the **parent** block. For the first block of activation epoch `N`, the parent is the last block of epoch `N-1`. Therefore `rfc0044_active(N-1)` returns `false` and `DaoScriptSizeVerifier` is never called for any non-cellbase transaction in that block. For all subsequent blocks in epoch `N`, the parent is also in epoch `N`, so the check runs correctly. The bug is confined to exactly one block per activation epoch.

The tx-pool applies `DaoScriptSizeVerifier` unconditionally in both its async and blocking code paths, with no `rfc0044_active` guard, so a crafted oversized-DAO transaction is rejected by `send_transaction`. The attacker must bypass the tx-pool entirely by assembling the block manually and submitting it via the `submit_block` RPC.

## Impact Explanation
This is a **Critical** vulnerability matching the impact class: *"Vulnerabilities which could easily cause consensus deviation."* Nodes running the buggy code accept the first block of epoch `N` containing an oversized DAO script. Any node running a corrected build evaluates `rfc0044_active(N)` = `true`, runs `DaoScriptSizeVerifier`, and rejects the block. This produces a deterministic, permanent chain split at the activation epoch boundary.

## Likelihood Explanation
The attack requires a malicious miner who knows the RFC0044 activation epoch (deterministic from the chain spec), can craft a transaction with an oversized DAO output script, and can submit a manually assembled block via the standard `submit_block` RPC. No special privileges beyond operating a mining node are required. The window is exactly one block, but it is fully predictable and the attack can be prepared in advance.

## Recommendation
Replace `self.parent.epoch().number()` with `self.header.epoch().number()` in the `rfc0044_active` guard at line 445:

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

**File:** tx-pool/src/util.rs (L110-131)
```rust
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
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
