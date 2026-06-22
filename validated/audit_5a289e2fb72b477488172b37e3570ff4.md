### Title
`TransactionTemplate.required` and `TransactionTemplate.depends` Fields Hardcoded as Unimplemented in Block Template RPC — (`tx-pool/src/block_assembler/mod.rs`)

---

### Summary

The `get_block_template` RPC response includes `TransactionTemplate` objects whose `required` and `depends` fields are explicitly hardcoded to `false` and `None` with `// unimplemented` comments. These fields are part of the block template protocol specification and are intended to communicate to miners which transactions are mandatory and what their dependency ordering is. The data structure fields exist and are serialized to callers, but the corresponding computation logic was never implemented — a direct structural analog to the USSD missing redeem feature.

---

### Finding Description

In `tx-pool/src/block_assembler/mod.rs`, the function `tx_entry_to_template` converts a `TxEntry` into a `TransactionTemplate` for the `get_block_template` RPC response:

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

The `required` field is intended to signal to a miner that a transaction **must** be included for the block to be valid. The `depends` field is intended to communicate the intra-template dependency graph so miners can correctly order transactions. Both are explicitly marked `// unimplemented` and are never computed from actual pool state.

The same pattern appears for uncle templates in `uncle_to_template`, where `required: false` is also hardcoded without any computation. [1](#0-0) 

The `TransactionTemplate` type is part of the public JSON-RPC API surface, serialized and returned to every caller of `get_block_template`. The fields are present in the wire format and documented as meaningful, but the node never populates them with real values. [2](#0-1) 

---

### Impact Explanation

**`required: false` (always):** Any miner or mining pool operator implementing a standards-compliant block template client that respects the `required` flag will treat every transaction as optional. If CKB ever introduces protocol-level mandatory transactions (e.g., for a soft-fork activation, a special protocol transaction, or a future upgrade), miners following the template protocol would be permitted to omit them, producing blocks that are invalid under the new rules. The node provides no mechanism to enforce inclusion via the template.

**`depends: None` (always):** A miner that reorders transactions within the template — a common optimization in custom mining clients — has no dependency graph to consult. Reordering transactions that have input/output dependencies (where transaction B spends an output of transaction A) would produce an invalid block. The node assembles transactions in a valid order internally, but that ordering is not communicated to the miner through the protocol field designed for exactly this purpose.

The structural parallel to the USSD finding is exact: the data structure fields (`required`, `depends`) exist, are serialized to callers, and are part of the documented protocol, but the implementation that would give them meaning was never written.

---

### Likelihood Explanation

Any unprivileged RPC caller (local miner, mining pool, or third-party mining client) invoking `get_block_template` receives the defective template. Mining clients that implement the full block template specification — particularly those that reorder transactions for fee optimization or that check `required` before omitting transactions — are directly affected. The entry path requires no special privileges: a standard `get_block_template` RPC call is sufficient.

---

### Recommendation

Implement `required` and `depends` in `tx_entry_to_template`:

1. **`required`**: Determine which transactions in the assembled template are mandatory for block validity (e.g., transactions that satisfy a proposal commitment window obligation) and set `required: true` for those entries.
2. **`depends`**: Walk the resolved transaction graph for each `TxEntry` and populate `depends` with the indices of other transactions in the template that must precede it.

Until implemented, the fields should either be removed from the public API or documented explicitly as always-false/always-null so mining client authors are not misled.

---

### Proof of Concept

The `// unimplemented` annotations at lines 988 and 990 of `tx-pool/src/block_assembler/mod.rs` are the direct evidence. A mining client that:

1. Calls `get_block_template`
2. Filters the transaction list to only those with `required: true` (a valid interpretation of the protocol)
3. Submits the resulting block

…will submit a block with zero non-cellbase transactions, since every transaction has `required: false`. No attacker-controlled input is needed; the defect is triggered by any conforming use of the RPC. [1](#0-0)

### Citations

**File:** tx-pool/src/block_assembler/mod.rs (L971-983)
```rust
pub(crate) fn uncle_to_template(uncle: &UncleBlockView) -> UncleTemplate {
    UncleTemplate {
        hash: uncle.hash().into(),
        required: false,
        proposals: uncle
            .data()
            .proposals()
            .into_iter()
            .map(Into::into)
            .collect(),
        header: uncle.data().header().into(),
    }
}
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
