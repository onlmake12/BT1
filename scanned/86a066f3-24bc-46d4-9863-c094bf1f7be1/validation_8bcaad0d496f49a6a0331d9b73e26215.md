### Title
`tx_entry_to_template` Hardcodes `required: false` and `depends: None` — Miners Customizing Block Templates May Produce Invalid Blocks - (`tx-pool/src/block_assembler/mod.rs`)

---

### Summary

The `tx_entry_to_template` function in the block assembler always emits `required: false` and `depends: None` for every transaction in the block template, with both fields explicitly annotated `// unimplemented`. The `BlockTemplate` API is documented to allow miners to remove transactions and add their own. Without dependency information, a miner who removes any transaction from the template may unknowingly retain a dependent transaction that spends the removed transaction's output, producing an invalid block and forfeiting their PoW reward.

---

### Finding Description

`tx_entry_to_template` converts a `TxEntry` from the tx-pool into a `TransactionTemplate` for the `get_block_template` RPC response:

```rust
// tx-pool/src/block_assembler/mod.rs
pub(crate) fn tx_entry_to_template(entry: &TxEntry) -> TransactionTemplate {
    TransactionTemplate {
        hash: entry.transaction().hash().into(),
        required: false, // unimplemented
        cycles: Some(entry.cycles.into()),
        depends: None, // unimplemented
        data: entry.transaction().data().into(),
    }
}
```

The `TransactionTemplate` type documents both fields as actionable guidance for miners:

- `required`: "Whether miner must include this transaction in the new block."
- `depends`: "This transaction can only be committed if its dependencies are also committed in the new block. This field is a list of indices into the array `transactions` in the block template."

The `BlockTemplate` type itself states: "Miners must include the transactions marked as `required` in the assembled new block." The `get_block_template` RPC documentation explicitly states the API is "designed to allow miners to remove transactions and adding new transactions to the block."

Because `depends` is always `None`, a miner who removes transaction A from the template has no way to know that transaction B (still in the template) spends A's output. The resulting block is invalid and will be rejected by consensus.

Because `required` is always `false`, the node can never signal to miners that a particular transaction must be included, even if future protocol logic or application-layer requirements demand it.

---

### Impact Explanation

A miner who calls `get_block_template`, removes one or more transactions to optimize fee selection, and then solves the PoW puzzle will submit a block that is rejected at consensus validation because a remaining transaction references a spent output that no longer exists in the block. The miner forfeits the entire block reward (primary issuance + secondary issuance + transaction fees) for that block. The financial loss scales with the current block reward. The network itself is not destabilized, but the miner suffers a direct, irreversible economic loss.

---

### Likelihood Explanation

The `get_block_template` RPC is the standard interface for all external miners and mining pools. The API is explicitly documented to support transaction removal. Any miner or mining pool that implements custom transaction selection logic (a common practice for fee optimization) and operates on a mempool containing chained transactions (transactions spending outputs of other unconfirmed transactions, which is a normal and frequent occurrence) will encounter this silently. The tx-pool already supports chained transactions, so the dependency scenario is not hypothetical.

---

### Recommendation

Populate `depends` in `tx_entry_to_template` by inspecting each transaction's inputs against the set of transactions already added to the template and recording the indices of any template transactions whose outputs are consumed. Populate `required` based on whatever policy the node wishes to enforce (e.g., transactions that are part of a chained group where omitting one invalidates others). Both fields are already defined in the type system and documented; only the production population logic is missing.

---

### Proof of Concept

1. Submit two chained transactions to the tx-pool: Tx A creates an output; Tx B spends that output.
2. Call `get_block_template`. Both A and B appear in `transactions`; B's `depends` is `null`.
3. Construct a block from the template, omitting Tx A (e.g., to save block space).
4. Solve PoW and call `submit_block`.
5. The node rejects the block because Tx B references a non-existent input (A's output was never committed).
6. The miner's PoW work is wasted and the block reward is lost. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** tx-pool/src/block_assembler/mod.rs (L985-993)
```rust
pub(crate) fn tx_entry_to_template(entry: &TxEntry) -> TransactionTemplate {
    TransactionTemplate {
        hash: entry.transaction().hash().into(),
        required: false, // unimplemented
        cycles: Some(entry.cycles.into()),
        depends: None, // unimplemented
        data: entry.transaction().data().into(),
    }
}
```

**File:** util/jsonrpc-types/src/block_template.rs (L62-69)
```rust
    /// Provided valid uncle blocks candidates for the new block.
    ///
    /// Miners must include the uncles marked as `required` in the assembled new block.
    pub uncles: Vec<UncleTemplate>,
    /// Provided valid transactions which can be committed in the new block.
    ///
    /// Miners must include the transactions marked as `required` in the assembled new block.
    pub transactions: Vec<TransactionTemplate>,
```

**File:** util/jsonrpc-types/src/block_template.rs (L228-255)
```rust
/// Transaction template which is ready to be committed in the new block.
#[derive(Clone, Default, Serialize, Deserialize, PartialEq, Eq, Hash, Debug, JsonSchema)]
pub struct TransactionTemplate {
    /// Transaction hash.
    pub hash: H256,
    /// Whether miner must include this transaction in the new block.
    pub required: bool,
    /// The hint of how many cycles this transaction consumes.
    ///
    /// Miners can utilize this field to ensure that the total cycles do not
    /// exceed the limit while selecting transactions.
    pub cycles: Option<Cycle>,
    /// Transaction dependencies.
    ///
    /// This is a hint to help miners selecting transactions.
    ///
    /// This transaction can only be committed if its dependencies are also committed in the new block.
    ///
    /// This field is a list of indices into the array `transactions` in the block template.
    ///
    /// For example, `depends = [1, 2]` means this transaction depends on
    /// `block_template.transactions[1]` and `block_template.transactions[2]`.
    pub depends: Option<Vec<Uint64>>,
    /// The transaction.
    ///
    /// Miners must keep it unchanged when including it in the new block.
    pub data: Transaction,
}
```
