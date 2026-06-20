### Title
Hardcoded `TRANSACTION_SIZE_LIMIT` in tx-pool permanently rejects consensus-valid transactions — (`util/types/src/core/tx_pool.rs`)

### Summary
The tx-pool enforces a hardcoded, non-configurable `TRANSACTION_SIZE_LIMIT = 512 * 1_000` bytes. Any transaction whose serialized size falls between 512 KB and the consensus block-size ceiling (~597 KB) is permanently rejected by every standard CKB node's tx-pool, even though it is fully valid under CKB consensus rules. No operator or user can override this limit.

### Finding Description
`TRANSACTION_SIZE_LIMIT` is defined as a compile-time constant:

```rust
// util/types/src/core/tx_pool.rs
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
``` [1](#0-0) 

It is enforced unconditionally in `tx-pool/src/util.rs` inside `non_contextual_verify`, which is called for every transaction submitted via RPC or received from a relay peer:

```rust
let tx_size = tx.data().serialized_size_in_block() as u64;
if tx_size > TRANSACTION_SIZE_LIMIT {
    return Err(Reject::ExceededTransactionSizeLimit(tx_size, TRANSACTION_SIZE_LIMIT));
}
``` [2](#0-1) 

The code comment at that site explicitly acknowledges: *"The ckb consensus does not limit the size of a single transaction"*. The consensus block-size ceiling is:

```rust
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT; // 597 * 1_000 = 597,000
``` [3](#0-2) 

The gap between 512,000 and 597,000 bytes is a range of transactions that:
- Pass all consensus validation rules (no per-transaction size limit exists in consensus)
- Are permanently rejected by the tx-pool on every standard node
- Cannot be submitted via RPC `send_transaction` or relayed via the P2P relay protocol

`TRANSACTION_SIZE_LIMIT` is absent from `TxPoolConfig` and has no corresponding TOML field, making it impossible for operators to adjust. [4](#0-3) 

The rejection propagates to the RPC caller as `PoolRejectedTransactionBySizeLimit`: [5](#0-4) 

### Impact Explanation
Any transaction author who constructs a consensus-valid transaction in the 512 KB–597 KB range will find it permanently unsubmittable through any standard CKB node. The transaction cannot enter the mempool, cannot be proposed, and therefore can never be committed to the chain. If a user's cells can only be spent by a transaction of this size (e.g., a large aggregation or a script requiring bulky witness data), those funds are effectively frozen — directly analogous to the Yearn `maxLoss = 1bps` issue where a hardcoded parameter permanently blocks withdrawals.

### Likelihood Explanation
Transactions of this size are uncommon but achievable: large multi-input aggregation transactions, transactions with bulky witness data (e.g., large Merkle proofs or batch-verification inputs embedded in witnesses), or transactions spending many cells simultaneously can reach this range. The likelihood is low in normal usage but non-negligible for advanced script authors, aggregators, or protocols that batch many operations into a single transaction.

### Recommendation
Either:
1. Expose `TRANSACTION_SIZE_LIMIT` as a configurable field in `TxPoolConfig` (analogous to `max_tx_verify_cycles`), allowing operators to raise it up to `MAX_BLOCK_BYTES`, or
2. Set the hardcoded value to `MAX_BLOCK_BYTES` so the tx-pool limit is never more restrictive than consensus.

### Proof of Concept
1. Construct a transaction whose `serialized_size_in_block()` is, e.g., 513,000 bytes (achievable by including many inputs each with a large witness, e.g., a script that embeds a large Merkle proof).
2. Confirm it passes `NonContextualTransactionVerifier` — no per-transaction size rule exists in consensus.
3. Submit via RPC `send_transaction`.
4. Observe rejection: `PoolRejectedTransactionBySizeLimit` / `ExceededTransactionSizeLimit(513000, 512000)`.
5. The transaction is consensus-valid but permanently unsubmittable on any standard node.

### Citations

**File:** util/types/src/core/tx_pool.rs (L305-309)
```rust
/// The maximum size of the tx-pool to accept transactions
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

**File:** spec/src/consensus.rs (L82-84)
```rust
/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** util/app-config/src/configs/tx_pool.rs (L10-43)
```rust
#[derive(Clone, Debug, Serialize)]
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
    /// txs with lower fee rate than this will not be relayed or be mined
    #[serde(with = "FeeRateDef")]
    pub min_fee_rate: FeeRate,
    /// txs need to pay larger fee rate than this for RBF
    #[serde(with = "FeeRateDef")]
    pub min_rbf_rate: FeeRate,
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
    #[serde(default = "default_max_tx_verify_workers")]
    pub max_tx_verify_workers: usize,
    /// max ancestors size limit for a single tx
    pub max_ancestors_count: usize,
    /// rejected tx time to live by days
    pub keep_rejected_tx_hashes_days: u8,
    /// rejected tx count limit
    pub keep_rejected_tx_hashes_count: u64,
    /// The file to persist the tx pool on the disk when tx pool have been shutdown.
    ///
    /// By default, it is a subdirectory of 'tx-pool' subdirectory under the data directory.
    #[serde(default)]
    pub persisted_data: PathBuf,
    /// The recent reject record database directory path.
    ///
    /// By default, it is a subdirectory of 'tx-pool' subdirectory under the data directory.
    #[serde(default)]
    pub recent_reject: PathBuf,
    /// The expiration time for pool transactions in hours
    pub expiry_hours: u8,
}
```

**File:** rpc/src/error.rs (L193-196)
```rust
            Reject::ExceededTransactionSizeLimit(_, _) => {
                RPCError::PoolRejectedTransactionBySizeLimit
            }
            Reject::Expiry(_) => RPCError::TransactionExpired,
```
