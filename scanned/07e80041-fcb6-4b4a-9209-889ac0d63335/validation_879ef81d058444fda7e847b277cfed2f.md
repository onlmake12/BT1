### Title
`tx_size_limit` Reported by `tx_pool_info` RPC Is a Hardcoded Constant That Does Not Reflect the Actual Consensus `max_block_bytes` Limit — (`util/types/src/core/tx_pool.rs`, `tx-pool/src/util.rs`, `tx-pool/src/service.rs`)

---

### Summary

The `tx_size_limit` field returned by the `tx_pool_info` RPC, and the admission gate used in `non_contextual_verify`, both rely on the hardcoded constant `TRANSACTION_SIZE_LIMIT = 512 * 1_000` bytes. This constant is never derived from the consensus `max_block_bytes` parameter. When `max_block_bytes` is configured below `TRANSACTION_SIZE_LIMIT` (a valid chain-spec option), the tx-pool will accept, fully verify, and retain transactions that can never be committed to any block, while simultaneously advertising an incorrect size ceiling to every RPC caller.

---

### Finding Description

`TRANSACTION_SIZE_LIMIT` is defined as a compile-time constant: [1](#0-0) 

The tx-pool's non-contextual admission check compares every incoming transaction against this constant, not against the live consensus value: [2](#0-1) 

The `tx_pool_info` RPC handler then copies the same constant verbatim into the `TxPoolInfo` struct that is returned to every caller: [3](#0-2) 

The consensus object carries the authoritative, configurable limit: [4](#0-3) 

The block verifier enforces that limit at block-acceptance time: [5](#0-4) 

The block assembler also reads `consensus.max_block_bytes()` directly when selecting transactions for a template: [6](#0-5) 

The two paths are therefore inconsistent: the assembler and verifier use the live consensus value; the admission gate and the RPC info field use a frozen constant.

---

### Impact Explanation

**Scenario A — `max_block_bytes` < `TRANSACTION_SIZE_LIMIT` (e.g., a chain spec sets `max_block_bytes = 100_000`):**

1. The tx-pool admission check passes for any transaction ≤ 512 KB.
2. The node fully verifies and stores such a transaction, consuming CPU and memory.
3. The block assembler computes `txs_size_limit = max_block_bytes - basic_block_size` (≈ 100 KB minus overhead), so the transaction is silently skipped during template construction.
4. The transaction can never be committed; it ages out or is evicted, wasting all verification work.
5. An attacker who knows this discrepancy can flood the pool with near-512 KB transactions, each of which passes admission but is permanently unpackageable, exhausting pool memory (`max_tx_pool_size`) and verification workers.

**Scenario B — default mainnet (`max_block_bytes = 597 KB`):**

The constant underestimates the true limit (512 KB < 597 KB). Transactions between 512 KB and 597 KB are rejected by the pool even though the consensus would allow them in a block. The `tx_size_limit` field misleads tooling and wallets into believing the ceiling is lower than it actually is. [7](#0-6) 

---

### Likelihood Explanation

The default mainnet `max_block_bytes` (597 KB) is larger than `TRANSACTION_SIZE_LIMIT` (512 KB), so Scenario A does not apply to mainnet today. However:

- The `max_block_bytes` field is a first-class, user-configurable chain-spec parameter (`[params] max_block_bytes = …`).
- Any operator running a private or test network with a tighter block-size budget triggers the dangerous path without any code change.
- An RPC caller (unprivileged, no authentication required) can observe the misleading `tx_size_limit` value via `tx_pool_info` and craft transactions that exploit the gap. [8](#0-7) 

---

### Recommendation

Replace the hardcoded constant in both the admission check and the `TxPoolInfo` construction with the live consensus value. The `TxPoolService` already holds a `consensus` reference:

```rust
// tx-pool/src/util.rs  — non_contextual_verify
let tx_size_limit = consensus.max_block_bytes();   // was: TRANSACTION_SIZE_LIMIT
if tx_size > tx_size_limit {
    return Err(Reject::ExceededTransactionSizeLimit(tx_size, tx_size_limit));
}
```

```rust
// tx-pool/src/service.rs  — info()
tx_size_limit: self.consensus.max_block_bytes(),   // was: TRANSACTION_SIZE_LIMIT
```

This mirrors the fix recommended in M-15: make the reported and enforced limit reflect the actual system constraint rather than a static approximation.

---

### Proof of Concept

1. Start a CKB node with a chain spec that sets `max_block_bytes = 200_000` (200 KB).
2. Call `tx_pool_info` via RPC — the response shows `"tx_size_limit": "0x7d000"` (512 KB), not 200 KB.
3. Submit a transaction whose serialized size is 300 KB (between 200 KB and 512 KB).
4. The admission check in `non_contextual_verify` passes (`300_000 ≤ 512_000`).
5. The transaction is verified and stored in the pool.
6. Call `get_block_template` — the transaction is absent from `transactions[]` because `txs_size_limit = 200_000 - basic_block_size` excludes it.
7. The transaction can never be committed; repeat step 3 to exhaust pool capacity. [1](#0-0) [2](#0-1) [3](#0-2) [9](#0-8)

### Citations

**File:** util/types/src/core/tx_pool.rs (L306-309)
```rust
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

**File:** tx-pool/src/util.rs (L64-73)
```rust
    // The ckb consensus does not limit the size of a single transaction,
    // but if the size of the transaction is close to the limit of the block,
    // it may cause the transaction to fail to be packed
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
```

**File:** tx-pool/src/service.rs (L1093-1095)
```rust
            last_txs_updated_at: tx_pool.pool_map.get_max_update_time(),
            tx_size_limit: TRANSACTION_SIZE_LIMIT,
            max_tx_pool_size: self.tx_pool_config.max_tx_pool_size as u64,
```

**File:** spec/src/consensus.rs (L82-84)
```rust
/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** spec/src/consensus.rs (L738-741)
```rust
    /// Maximum number of bytes to use for the entire block
    pub fn max_block_bytes(&self) -> u64 {
        self.max_block_bytes
    }
```

**File:** verification/src/block_verifier.rs (L251-262)
```rust
    pub fn verify(&self, block: &BlockView) -> Result<(), Error> {
        // Skip bytes limit on genesis block
        if block.is_genesis() {
            return Ok(());
        }
        let block_bytes = block.data().serialized_size_without_uncle_proposals() as u64;
        if block_bytes <= self.block_bytes_limit {
            Ok(())
        } else {
            Err(BlockErrorKind::ExceededMaximumBlockBytes.into())
        }
    }
```

**File:** tx-pool/src/block_assembler/mod.rs (L184-212)
```rust
        let consensus = current.snapshot.consensus();
        let max_block_bytes = consensus.max_block_bytes() as usize;

        let current_template = &current.template;
        let uncles = &current_template.uncles;

        let (proposals, txs, basic_size) = {
            let tx_pool_reader = tx_pool.read().await;
            if current.snapshot.tip_hash() != tx_pool_reader.snapshot().tip_hash() {
                return Ok(());
            }

            let proposals =
                tx_pool_reader.package_proposals(consensus.max_block_proposals_limit(), uncles);

            let basic_size = Self::basic_block_size(
                current_template.cellbase.data(),
                uncles,
                proposals.iter(),
                current_template.extension.clone(),
            );

            let txs_size_limit = max_block_bytes
                .checked_sub(basic_size)
                .ok_or(BlockAssemblerError::Overflow)?;

            let max_block_cycles = consensus.max_block_cycles();
            let (txs, _txs_size, _cycles) =
                tx_pool_reader.package_txs(max_block_cycles, txs_size_limit);
```

**File:** spec/src/lib.rs (L201-205)
```rust
    /// The max_block_bytes
    ///
    /// See [`max_block_bytes`](consensus/struct.Consensus.html#structfield.max_block_bytes)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_block_bytes: Option<u64>,
```
