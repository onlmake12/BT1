### Title
DepGroup Sub-Cell `OutPointError::Unknown` Misclassified as Missing Input, Causing Irresolvable Orphan Pool Pollution — (`tx-pool/src/util.rs`, `tx-pool/src/component/orphan.rs`)

---

### Summary

`is_missing_input()` treats **any** `OutPointError::Unknown` as a missing transaction input, without distinguishing whether the unknown out-point came from a transaction input or from a DepGroup sub-cell. When a transaction references a live DepGroup cell whose `OutPointVec` data encodes a sub-out-point that is unknown to the node, `resolve_transaction` returns `OutPointError::Unknown` for that sub-cell. `is_missing_input()` returns `true`, and the transaction is added to the orphan pool. The orphan pool's resolution mechanism indexes and resolves orphans exclusively by **input** out-points — it will never resolve an orphan whose blocking reference is a dep group sub-cell. The orphan occupies a slot until time-based expiry, and an attacker can continuously re-send to maintain saturation of all 100 orphan pool slots.

---

### Finding Description

**Step 1 — `resolve_transaction` returns `OutPointError::Unknown` for unknown dep group sub-cells.**

In `resolve_transaction_dep`, when a `DepGroup` cell dep is processed, each sub-out-point is resolved via `cell_resolver`. If the sub-cell is unknown to the snapshot, `CellStatus::Unknown` is returned and the function propagates `OutPointError::Unknown(sub_out_point)`: [1](#0-0) 

This is confirmed by an existing test: [2](#0-1) 

**Step 2 — `is_missing_input()` does not distinguish input-unknown from dep-unknown.** [3](#0-2) 

`is_unknown()` matches any `OutPointError::Unknown(_)` regardless of origin: [4](#0-3) 

**Step 3 — The transaction is added to the orphan pool.**

In `after_process`, when `is_missing_input(reject)` is true for a remote-relayed transaction, it is unconditionally added to the orphan pool: [5](#0-4) 

**Step 4 — The orphan pool indexes and resolves orphans by input out-points only.**

`add_orphan_tx` only inserts entries into `by_out_point` for `tx.input_pts_iter()` — the transaction's inputs, not its dep group sub-cells: [6](#0-5) 

`find_by_previous` looks up orphans by matching a confirmed transaction's output out-points against `by_out_point`: [7](#0-6) 

Since the blocking reference is a dep group sub-cell (not an input), it is never registered in `by_out_point`. No confirmed transaction will ever trigger resolution of this orphan via `process_orphan_tx`.

**Step 5 — Bounded but persistent impact.**

The orphan pool is capped at `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` with random eviction when full, and entries expire after `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL`: [8](#0-7) 

An attacker who continuously re-relays crafted transactions can keep all 100 slots occupied with irresolvable entries, evicting legitimate orphans via random eviction.

---

### Impact Explanation

- All 100 orphan pool slots can be permanently occupied by irresolvable entries (until expiry, which the attacker can reset by re-sending).
- Legitimate orphan transactions (e.g., child-before-parent relay) are randomly evicted when the pool is full, breaking normal orphan resolution for honest peers.
- The `process_orphan_tx` BFS loop is never triggered for these entries, so they consume slots without ever being promoted or cleaned up by normal flow.

---

### Likelihood Explanation

The attack requires the attacker to first create a live DepGroup cell on-chain whose `OutPointVec` data encodes a non-existent sub-out-point. No consensus rule prevents this — a cell's data is arbitrary bytes, and the sub-out-points are only validated when the DepGroup is used as a dep. After paying the one-time on-chain cost (cell capacity), the attacker can relay an unlimited number of transactions referencing this DepGroup, each occupying one orphan pool slot. The attack is low-cost to sustain and requires no privileged access.

---

### Recommendation

`is_missing_input()` should be narrowed to only return `true` when the `OutPointError::Unknown` originated from resolving a **transaction input**, not a dep group sub-cell. One approach: propagate context through the error (e.g., a wrapper enum distinguishing `UnknownInput` from `UnknownDep`), or check the error's out-point against the transaction's input set before classifying it as a missing-input condition. Transactions with unknown dep group sub-cells should be rejected outright (not orphaned), since no incoming parent transaction can ever resolve a dep reference.

---

### Proof of Concept

1. Publish a cell on-chain with `data = OutPointVec([<random_nonexistent_outpoint>])` — this is a valid DepGroup cell with an invalid sub-reference.
2. Craft a transaction `T` with `cell_deps = [CellDep { out_point: <above cell>, dep_type: DepGroup }]` and any valid inputs/outputs.
3. Relay `T` to the target node via P2P relay (RelayV3 protocol).
4. Observe: `resolve_transaction` returns `OutPointError::Unknown(<random_nonexistent_outpoint>)` → `is_missing_input` returns `true` → `T` is added to the orphan pool.
5. Confirm: `process_orphan_tx` is never triggered for `T` regardless of what transactions are confirmed, because `T`'s inputs are valid and `by_out_point` has no entry for the dep sub-cell.
6. Repeat with 100 distinct crafted transactions to fill all orphan pool slots. Observe that legitimate orphan transactions are randomly evicted.

### Citations

**File:** util/types/src/core/cell.rs (L829-831)
```rust
        for sub_out_point in sub_out_points.into_iter() {
            resolved_cell_deps.push(cell_resolver(&sub_out_point, eager_load)?);
        }
```

**File:** util/types/src/core/tests/cell.rs (L183-210)
```rust
#[test]
fn resolve_transaction_resolve_dep_group_failed_because_unknown_sub_cell() {
    let mut cell_provider = CellMemoryDb::default();
    let header_checker = BlockHeadersChecker::default();

    let op_unknown = OutPoint::new(h256!("0x45").into(), 5);
    let op_dep = OutPoint::new(Byte32::zero(), 72);
    let cell_data = Into::<packed::OutPointVec>::into(vec![op_unknown.clone()]).as_bytes();
    let dep_group_cell = generate_dummy_cell_meta_with_data(cell_data);
    cell_provider
        .cells
        .insert(op_dep.clone(), Some(dep_group_cell));

    let dep = CellDep::new_builder()
        .out_point(op_dep)
        .dep_type(DepType::DepGroup)
        .build();

    let transaction = TransactionBuilder::default().cell_dep(dep).build();
    let mut seen_inputs = HashSet::new();
    let result = resolve_transaction(
        transaction,
        &mut seen_inputs,
        &cell_provider,
        &header_checker,
    );
    assert_error_eq!(result.unwrap_err(), OutPointError::Unknown(op_unknown),);
}
```

**File:** tx-pool/src/util.rs (L150-152)
```rust
pub(crate) fn is_missing_input(reject: &Reject) -> bool {
    matches!(reject, Reject::Resolve(out_point_err) if out_point_err.is_unknown())
}
```

**File:** util/types/src/core/error.rs (L267-271)
```rust
impl OutPointError {
    /// Returns true if the error is an unknown out_point.
    pub fn is_unknown(&self) -> bool {
        matches!(self, OutPointError::Unknown(_))
    }
```

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
```

**File:** tx-pool/src/component/orphan.rs (L15-16)
```rust
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L150-155)
```rust
        for out_point in tx.input_pts_iter() {
            self.by_out_point
                .entry(out_point)
                .or_default()
                .insert(tx.proposal_short_id());
        }
```

**File:** tx-pool/src/component/orphan.rs (L161-167)
```rust
    pub fn find_by_previous(&self, tx: &TransactionView) -> Vec<&ProposalShortId> {
        tx.output_pts()
            .iter()
            .filter_map(|out_point| self.by_out_point.get(out_point))
            .flatten()
            .collect::<Vec<_>>()
    }
```
