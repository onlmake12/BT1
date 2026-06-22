### Title
`TransactionTemplate.depends` and `TransactionTemplate.required` Never Populated in `get_block_template` RPC — (`tx-pool/src/block_assembler/mod.rs`)

---

### Summary

The `get_block_template` RPC is explicitly documented as allowing miners to remove transactions from the template before assembling a block. Two fields in `TransactionTemplate` — `required` and `depends` — are the documented mechanism for miners to do this safely. Both are hardcoded to stub values (`false` and `None`) with explicit `// unimplemented` comments in production code, making the transaction-selection feature of `get_block_template` permanently broken for any miner that relies on these fields.

---

### Finding Description

In `tx-pool/src/block_assembler/mod.rs`, the function `tx_entry_to_template` converts a pool entry into the JSON `TransactionTemplate` returned to miners via `get_block_template`:

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
``` [1](#0-0) 

The `TransactionTemplate` type documents these fields as:

- `required: bool` — "Whether miner must include this transaction in the new block."
- `depends: Option<Vec<Uint64>>` — "This transaction can only be committed if its dependencies are also committed in the new block. This field is a list of indices into the array `transactions` in the block template." [2](#0-1) 

The `BlockTemplate` type further states: "Miners must include the transactions marked as `required` in the assembled new block." [3](#0-2) 

The RPC documentation explicitly states the design intent: "The RPC is designed to allow miners to remove transactions and adding new transactions to the block." [4](#0-3) 

Because `depends` is always `None`, a miner that attempts to selectively remove transactions from the template has no way to know which transactions have intra-block dependencies. CKB allows transactions within the same block to spend outputs of other transactions in the same block. If a miner removes a parent transaction (tx_A) but retains a child transaction (tx_B that spends an output of tx_A), the assembled block is invalid and will be rejected by `submit_block`.

---

### Impact Explanation

Any miner that implements transaction selection logic based on the documented `depends` field — which is the only safe way to remove transactions per the protocol spec — will silently produce invalid blocks. The `submit_block` call will fail, and the miner wastes all PoW work expended on that block. The `required: false` stub additionally means miners can never be told which transactions are mandatory, breaking any future protocol extension that relies on this field.

---

### Likelihood Explanation

The official CKB miner binary does not use `depends` or `required` (it submits all template transactions unchanged), so the default miner is unaffected. However, any third-party mining pool or miner software that implements transaction selection — a use case the RPC explicitly advertises and documents — will be affected. A transaction submitter (unprivileged) can trivially trigger the scenario by submitting a chain of dependent transactions (tx_A → tx_B) to the tx pool; both will appear in the block template with `depends: None`, giving no signal to the miner about the dependency.

---

### Recommendation

Implement `depends` in `tx_entry_to_template` by computing the set of indices into the template's transaction array that each entry depends on. The `TxEntry` already carries dependency information through the pool's internal graph. Similarly, implement `required` if any protocol-level mandatory transactions are ever introduced. Remove the `// unimplemented` stubs and replace them with correct logic.

---

### Proof of Concept

1. Submit two transactions to the tx pool: tx_A (spends a confirmed cell) and tx_B (spends an output of tx_A).
2. Wait for both to enter the proposed pool and appear in `get_block_template`.
3. Observe that tx_B's `TransactionTemplate` has `"depends": null` and `"required": false`.
4. A miner removes tx_A from the template (e.g., because it has a lower fee rate) while keeping tx_B.
5. The miner solves PoW and calls `submit_block`.
6. The node rejects the block because tx_B references an unresolved input (tx_A's output is not in the block).

The root cause is entirely in `tx_entry_to_template` at `tx-pool/src/block_assembler/mod.rs:985–993`, which is called unconditionally for every transaction in every block template. [5](#0-4)

### Citations

**File:** tx-pool/src/block_assembler/mod.rs (L782-786)
```rust
            transactions: template
                .transactions
                .iter()
                .map(tx_entry_to_template)
                .collect(),
```

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

**File:** util/jsonrpc-types/src/block_template.rs (L66-69)
```rust
    /// Provided valid transactions which can be committed in the new block.
    ///
    /// Miners must include the transactions marked as `required` in the assembled new block.
    pub transactions: Vec<TransactionTemplate>,
```

**File:** util/jsonrpc-types/src/block_template.rs (L230-255)
```rust
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

**File:** rpc/src/module/miner.rs (L26-30)
```rust
    /// Returns block template for miners.
    ///
    /// Miners can assemble the new block from the template. The RPC is designed to allow miners
    /// to remove transactions and adding new transactions to the block.
    ///
```
