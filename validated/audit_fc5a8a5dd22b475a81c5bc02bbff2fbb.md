### Title
Inconsistent `DaoScriptSizeVerifier` Application Between Tx-Pool and Block Verification Allows Miners to Bypass DAO Lock Script Size Restriction Before RFC0044 Activation — (File: `verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

The `DaoScriptSizeVerifier` is invoked **unconditionally** in the tx-pool admission path but is **gated by `rfc0044_active`** in the block verification path. Before epoch 8651 on mainnet (epoch 5711 on testnet), a miner could directly include a DAO phase-1 withdrawal transaction with a mismatched lock script size in a block, bypassing the tx-pool rejection and earning excess DAO interest. The code inconsistency remains in the repository today.

---

### Finding Description

**Two call sites, two different rules:**

**Tx-pool** (`tx-pool/src/util.rs`, lines 111–128): `DaoScriptSizeVerifier` is called unconditionally after `ContextualTransactionVerifier` succeeds, for every transaction submitted to the pool. [1](#0-0) 

**Block verification** (`verification/contextual/src/contextual_block_verifier.rs`, lines 444–453): `DaoScriptSizeVerifier` is only called when `rfc0044_active` returns `true` for the parent block's epoch number. [2](#0-1) 

`rfc0044_active` is hardcoded to epoch 8651 on mainnet and epoch 5711 on testnet: [3](#0-2) [4](#0-3) 

**What `DaoScriptSizeVerifier` enforces:**

The verifier requires that a DAO deposit cell (phase-1 input, identified by all-zero 8-byte data) and its corresponding withdrawal output cell use lock scripts of **identical byte size**. Cells deposited before `starting_block_limiting_dao_withdrawing_lock` (block 10,000,000 on mainnet) are exempt via an inner block-number check: [5](#0-4) 

**The inconsistency:**

| Path | Before epoch 8651 | After epoch 8651 |
|---|---|---|
| Tx-pool admission | `DaoScriptSizeVerifier` **applied** | `DaoScriptSizeVerifier` **applied** |
| Block verification | `DaoScriptSizeVerifier` **skipped** | `DaoScriptSizeVerifier` **applied** |

Before epoch 8651, a miner can construct a phase-1 DAO withdrawal transaction where the output cell uses a **smaller** lock script than the deposit cell, include it directly in a block (bypassing the tx-pool), and have it accepted by all nodes. The tx-pool would reject the same transaction if submitted normally.

---

### Impact Explanation

**Financial exploit via occupied-capacity manipulation:**

In `DaoCalculator::calculate_maximum_withdraw`, the maximum withdrawal amount is computed as:

```
counted_capacity = withdrawal_cell.capacity - withdrawal_cell.occupied_capacity
withdraw_capacity = counted_capacity * (withdrawing_ar / deposit_ar) + occupied_capacity
``` [6](#0-5) 

If a user deposits with a large lock script (e.g., 100 bytes) and the miner includes a phase-1 transaction that changes the lock script to a small one (e.g., 20 bytes), the withdrawal cell's `occupied_capacity` is reduced. This increases `counted_capacity`, which earns DAO interest, allowing the user to claim more interest than they are entitled to. The on-chain DAO script does not independently enforce lock script size equality — the node-level `DaoScriptSizeVerifier` is the sole enforcement mechanism (described in code as "a temporary solution till Nervos DAO script can be properly upgraded"). [7](#0-6) 

**Impact: Medium** — financial gain is bounded by the lock script size difference (shannons of extra interest per byte), but the bypass of a consensus-level rule is a protocol integrity issue.

---

### Likelihood Explanation

**Likelihood: Medium**

- The attacker must be a miner (or collude with one) to include the transaction directly in a block, bypassing the tx-pool. Miners are explicitly in scope per the bounty rules.
- The window is closed on mainnet (epoch 8651 reached) and testnet (epoch 5711 reached). For devnet, `rfc0044_active_epoch = 0` so the check is always applied. The inconsistency remains in the codebase and would affect any new chain with a non-zero `rfc0044_active_epoch` that has not yet reached activation.
- No special key or privileged operator access beyond mining is required. [8](#0-7) 

---

### Recommendation

Remove the `rfc0044_active` gate from the block verification path and call `DaoScriptSizeVerifier` unconditionally in `BlockTxsVerifier::verify`, matching the tx-pool behavior. The inner `starting_block_limiting_dao_withdrawing_lock` exemption inside the verifier already handles the granular block-number-based exemption for pre-existing cells. [2](#0-1) 

---

### Proof of Concept

1. On a chain where `rfc0044_active_epoch` has not yet been reached (e.g., a fresh chain with a high activation epoch), a user deposits CKB into the DAO using a lock script of size N bytes (e.g., 100 bytes, using a padded args field).
2. The user constructs a phase-1 withdrawal transaction where the output cell uses a lock script of size M < N bytes (e.g., 20 bytes, a standard secp256k1 lock).
3. Submitting this transaction to the tx-pool is rejected: `DaoScriptSizeVerifier` fires unconditionally and returns `TransactionError::DaoLockSizeMismatch`. [9](#0-8) 

4. A miner directly assembles a block containing this transaction. Block verification calls `BlockTxsVerifier::verify`, which checks `rfc0044_active(parent.epoch().number())` — this returns `false` before the activation epoch, so `DaoScriptSizeVerifier` is **not called**.
5. The block is accepted by all nodes. The phase-1 withdrawal cell with the smaller lock script is now on-chain.
6. In phase 2, `DaoCalculator::calculate_maximum_withdraw` computes interest using the smaller `occupied_capacity` of the withdrawal cell, yielding a larger `counted_capacity` and thus excess interest paid out to the attacker. [6](#0-5)

### Citations

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

**File:** util/constant/src/softfork/mainnet.rs (L1-4)
```rust
/// hardcode RFC0044 active epoch
pub const RFC0044_ACTIVE_EPOCH: u64 = 8651;


```

**File:** verification/src/transaction_verifier.rs (L817-819)
```rust
/// Verifies that deposit cell and withdrawing cell in Nervos DAO use same sized lock scripts.
/// It provides a temporary solution till Nervos DAO script can be properly upgraded.
pub struct DaoScriptSizeVerifier<DL> {
```

**File:** verification/src/transaction_verifier.rs (L872-887)
```rust
            // Only cells committed after the pre-defined block number in consensus is
            // applied to this rule
            if let Some(info) = &input_meta.transaction_info
                && info.block_number
                    < self
                        .consensus
                        .starting_block_limiting_dao_withdrawing_lock()
            {
                continue;
            }

            // Now we have a pair of DAO deposit and withdrawing cells, it is expected
            // they have the lock scripts of the same size.
            if input_meta.cell_output.lock().total_size() != cell_output.lock().total_size() {
                return Err((TransactionError::DaoLockSizeMismatch { index: i }).into());
            }
```

**File:** util/dao/src/lib.rs (L149-158)
```rust
        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

        Ok(withdraw_capacity)
```
