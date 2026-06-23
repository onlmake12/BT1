### Title
Tx-Pool Cell-Dep Admission Permanently Blocked by Concurrent Spend in Pool — (`tx-pool/src/pool_cell.rs`)

### Summary
The CKB tx-pool marks a cell as `Dead` the moment any transaction in the pool spends it. This causes every transaction that references the same cell as a `cell_dep` to be rejected with `OutPointError::Dead`, even though the consensus rules permit both transactions to coexist in the same block (provided the cell-dep user is ordered before the spender). The pool's own integration test acknowledges this as "current tx-pool implementation limitation but not consensus rule." An unprivileged RPC caller who controls the cell can exploit this ordering asymmetry to permanently suppress any transaction that depends on that cell as a code/data reference.

### Finding Description
`PoolCell::cell()` in `tx-pool/src/pool_cell.rs` is the sole cell-status oracle used when resolving a new transaction against the in-pool state:

```rust
// pool_cell.rs lines 19-31
fn cell(&self, out_point: &OutPoint, _eager_load: bool) -> CellStatus {
    if !self.rbf && self.pool_map.edges.get_input_ref(out_point).is_some() {
        return CellStatus::Dead;   // ← fires for ANY pool input reference
    }
    if let Some((output, data)) = self.pool_map.get_output_with_data(out_point) {
        ...
        CellStatus::live_cell(cell_meta)
    } else {
        CellStatus::Unknown
    }
}
``` [1](#0-0) 

The check `edges.get_input_ref(out_point).is_some()` fires whenever the out-point appears as an **input** in any pool entry — it does not distinguish between a cell being consumed as an input versus being read as a `cell_dep`. Consequently, `resolve_transaction` (called from `resolve_tx_from_pool`) receives `CellStatus::Dead` for the dep cell and returns `OutPointError::Dead`, causing the submission to be rejected: [2](#0-1) [3](#0-2) 

The consensus layer has no such restriction. The integration test `CellBeingCellDepThenSpentInSameBlockTestSubmitBlock` proves that a block ordering `[C, B]` (cell-dep user first, spender second) is **valid**, while `CellBeingSpentThenCellDepInSameBlockTestSubmitBlock` proves `[B, C]` is **invalid**. The test for the pool-submission path explicitly notes:

```
// NOTE: It MUST submit C before B. If submit C after B, the proposed pool will reject C as
// it thinks that B has already spent A; A is one of C's cell-deps; hence C is invalid. This
// is current tx-pool implementation limitation but not consensus rule.
``` [4](#0-3) [5](#0-4) 

### Impact Explanation
Any transaction C that references cell A as a `cell_dep` is **permanently rejected** from the pool for as long as any transaction B that spends A remains in the pool. Because the pool has no mechanism to re-evaluate C once B is eventually evicted or committed, C must be resubmitted from scratch. If B is kept alive in the pool (e.g., with a fee just above the eviction threshold), C is effectively censored for an extended window. For time-sensitive operations — claiming epoch-bounded rewards, satisfying `since`-locked inputs, or participating in on-chain auctions — this window can translate directly into irreversible economic loss for the submitter of C.

### Likelihood Explanation
The attacker must control cell A (i.e., hold the key satisfying A's lock script) and submit a valid spend transaction B. This is realistic for:
- Script authors who deploy code cells and later wish to suppress transactions that invoke their old code
- Any party that owns a cell widely used as a shared `cell_dep` (e.g., a library cell referenced by many UDT issuance transactions)

The attack requires no privileged node access, no majority hash power, and no Sybil capability — only the ability to call `send_transaction` via the public RPC.

### Recommendation
- **Short term**: In `PoolCell::cell()`, return `CellStatus::Dead` only when the out-point is consumed as an **input** by a pool transaction *and* the querying context is also an input resolution. For `cell_dep` resolution (the `eager_load` path), return `CellStatus::Live` if the cell exists on-chain, regardless of pool-input edges. Track the ordering dependency so the block assembler places C before B.
- **Long term**: Redesign the pool's edge-tracking to distinguish input-consumption edges from cell-dep-reference edges, mirroring the consensus rule that a cell may be both a dep and a later-spent input within the same block provided ordering is respected.

### Proof of Concept
1. Mine enough blocks so cell A (output of a committed transaction) is live on-chain.
2. Construct transaction B that spends A (satisfying A's lock script).
3. Submit B via `send_transaction` RPC → B enters the pending pool; `pool_map.edges` records A as consumed.
4. Construct transaction C that uses A as a `cell_dep` (independent inputs, independent outputs).
5. Submit C via `send_transaction` RPC.
6. **Result**: C is rejected with `TransactionFailedToResolve / OutPointError::Dead(A)`, even though the block `[C, B]` would be consensus-valid.
7. C remains rejected for the entire lifetime of B in the pool. If B is a low-fee transaction near the eviction boundary, C can be suppressed for many blocks, causing any time-sensitive logic in C to miss its window. [1](#0-0) [2](#0-1) [6](#0-5)

### Citations

**File:** tx-pool/src/pool_cell.rs (L19-31)
```rust
    fn cell(&self, out_point: &OutPoint, _eager_load: bool) -> CellStatus {
        if !self.rbf && self.pool_map.edges.get_input_ref(out_point).is_some() {
            return CellStatus::Dead;
        }
        if let Some((output, data)) = self.pool_map.get_output_with_data(out_point) {
            let cell_meta = CellMetaBuilder::from_cell_output(output, data)
                .out_point(out_point.to_owned())
                .build();
            CellStatus::live_cell(cell_meta)
        } else {
            CellStatus::Unknown
        }
    }
```

**File:** tx-pool/src/pool.rs (L372-384)
```rust
    pub(crate) fn resolve_tx_from_pool(
        &self,
        tx: TransactionView,
        rbf: bool,
    ) -> Result<Arc<ResolvedTransaction>, Reject> {
        let snapshot = self.snapshot();
        let pool_cell = PoolCell::new(&self.pool_map, rbf);
        let provider = OverlayCellProvider::new(&pool_cell, snapshot);
        let mut seen_inputs = HashSet::new();
        resolve_transaction(tx, &mut seen_inputs, &provider, snapshot)
            .map(Arc::new)
            .map_err(Reject::Resolve)
    }
```

**File:** util/types/src/core/cell.rs (L706-714)
```rust
                    let cell_status = cell_provider.cell(out_point, eager_load);
                    match cell_status {
                        CellStatus::Dead => Err(OutPointError::Dead(out_point.clone())),
                        CellStatus::Unknown => Err(OutPointError::Unknown(out_point.clone())),
                        CellStatus::Live(cell_meta) => {
                            entry.insert(cell_meta.clone());
                            Ok(cell_meta)
                        }
                    }
```

**File:** test/src/specs/tx_pool/dead_cell_deps.rs (L22-81)
```rust
pub struct CellBeingCellDepThenSpentInSameBlockTestSubmitBlock;

impl Spec for CellBeingCellDepThenSpentInSameBlockTestSubmitBlock {
    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &nodes[0];

        let initial_inputs = gen_spendable(node0, 2);
        let input_a = &initial_inputs[0];
        let input_c = &initial_inputs[1];

        // Commit transaction A
        let tx_a = {
            let tx_a = always_success_transaction(node0, input_a);
            node0.submit_transaction(&tx_a);
            node0.mine_until_bool(|| is_transaction_committed(node0, &tx_a));
            tx_a
        };

        // Create transaction B which spends A
        let tx_b = {
            let input =
                CellMetaBuilder::from_cell_output(tx_a.output(0).unwrap(), Default::default())
                    .out_point(OutPoint::new(tx_a.hash(), 0))
                    .build();
            always_success_transaction(node0, &input)
        };

        // Create transaction C which depends A
        let tx_c = {
            let tx = always_success_transaction(node0, input_c);
            let cell_dep_to_tx_a = CellDepBuilder::default()
                .dep_type(DepType::Code)
                .out_point(OutPoint::new(tx_a.hash(), 0))
                .build();
            tx.as_advanced_builder().cell_dep(cell_dep_to_tx_a).build()
        };

        // Propose B and C, to prepare testing
        let block = node0
            .new_block_builder(None, None, None)
            .proposal(tx_b.proposal_short_id())
            .proposal(tx_c.proposal_short_id())
            .build();
        node0.submit_block(&block);
        node0.mine(node0.consensus().tx_proposal_window().closest());

        // Create block commits B and C in order
        let block = node0
            .new_block_builder(None, None, None)
            .transactions(vec![tx_c, tx_b])
            .build();

        let ret = node0
            .rpc_client()
            .submit_block("".to_owned(), block.data().into());
        assert!(
            ret.is_ok(),
            "a block commits transactions [C, B] should be valid, ret: {ret:?}"
        );
    }
```

**File:** test/src/specs/tx_pool/dead_cell_deps.rs (L84-154)
```rust
/// There are 3 transactions, A, B and C:
///   - A was already committed before;
///   - B spends A;
///   - A is one of C's cell-deps.
///
/// A block, which commits B and C in order, should be invalid because that C's cell-dep A is dead
/// (as C spends A, A is dead).
///
/// The difference between case `CellBeingSpentThenCellDepInSameBlockTestSubmitBlock` is the order
/// of committed transactions. This case commits `[B, C]`.
pub struct CellBeingSpentThenCellDepInSameBlockTestSubmitBlock;

impl Spec for CellBeingSpentThenCellDepInSameBlockTestSubmitBlock {
    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &nodes[0];

        let initial_inputs = gen_spendable(node0, 2);
        let input_a = &initial_inputs[0];
        let input_c = &initial_inputs[1];

        // Commit transaction A
        let tx_a = {
            let tx_a = always_success_transaction(node0, input_a);
            node0.submit_transaction(&tx_a);
            node0.mine_until_bool(|| is_transaction_committed(node0, &tx_a));
            tx_a
        };

        // Create transaction B which spends A
        let tx_b = {
            let input =
                CellMetaBuilder::from_cell_output(tx_a.output(0).unwrap(), Default::default())
                    .out_point(OutPoint::new(tx_a.hash(), 0))
                    .build();
            always_success_transaction(node0, &input)
        };

        // Create transaction C which depends A
        let tx_c = {
            let tx = always_success_transaction(node0, input_c);
            let cell_dep_to_tx_a = CellDepBuilder::default()
                .dep_type(DepType::Code)
                .out_point(OutPoint::new(tx_a.hash(), 0))
                .build();
            tx.as_advanced_builder().cell_dep(cell_dep_to_tx_a).build()
        };

        // Propose B and C, to prepare testing
        let block = node0
            .new_block_builder(None, None, None)
            .proposal(tx_b.proposal_short_id())
            .proposal(tx_c.proposal_short_id())
            .build();
        node0.submit_block(&block);
        node0.mine(node0.consensus().tx_proposal_window().closest());

        // Create block commits B and C in order
        let block = node0
            .new_block_builder(None, None, None)
            .transactions(vec![tx_b, tx_c])
            .build();

        let ret = node0
            .rpc_client()
            .submit_block("".to_owned(), block.data().into());
        assert!(
            ret.is_err(),
            "a block commits transactions [B, C] should be invalid, ret: {ret:?}"
        );
    }
}
```

**File:** test/src/specs/tx_pool/dead_cell_deps.rs (L267-273)
```rust
        // Submit B and C
        //
        // NOTE: It MUST submit C before B. If submit C after B, the proposed pool will reject C as
        // it thinks that B has already spent A; A is one of C's cell-deps; hence C is invalid. This
        // is current tx-pool implementation limitation but not consensus rule.
        node0.submit_transaction(&tx_c);
        node0.submit_transaction(&tx_b);
```
