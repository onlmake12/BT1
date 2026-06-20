### Title
RPC Handler Panic via Unvalidated `parent_hash` in `calculate_dao_field` — (File: `rpc/src/module/test.rs`)

---

### Summary

The `calculate_dao_field` and `generate_block_with_template` RPC handlers in the `IntegrationTestRpc` module call `.expect()` on the result of `get_block_header()` using an attacker-controlled `parent_hash`. When the hash is absent from the database, the `.expect()` panics, bypassing the `Result`-based error handling entirely — a direct structural analog to the external report's root cause where internal function calls escape the top-level error boundary.

---

### Finding Description

In `rpc/src/module/test.rs`, the `calculate_dao_field` function is exposed as a standalone RPC method and is also called internally by `generate_block_with_template`.

At line 722–724, the function performs:

```rust
let parent_header = snapshot
    .get_block_header(&(&block_template.parent_hash).into())
    .expect("parent header should be stored");
``` [1](#0-0) 

The `block_template.parent_hash` is directly supplied by the RPC caller. If the caller provides any hash not present in the node's database, `get_block_header` returns `None`, and `.expect("parent header should be stored")` **panics**.

A second panic site exists at lines 756–759:

```rust
DaoCalculator::new(consensus, &snapshot.borrow_as_data_loader())
    .dao_field(rtxs.iter(), &parent_header)
    .expect("dao calculation should be OK")
    .into(),
``` [2](#0-1) 

The function's declared return type is `Result<Byte32>`, which implies all failure paths should be returned as `Err(...)`. However, panics are not `Result` values — they unwind the stack and bypass the `?` operator entirely. This is structurally identical to the external report: the top-level error boundary (`Result` / try-catch) is present, but internal calls escape it.

The caller `generate_block_with_template` uses `?` to propagate errors from `calculate_dao_field`:

```rust
fn generate_block_with_template(&self, block_template: BlockTemplate) -> Result<H256> {
    let dao_field = self.calculate_dao_field(block_template.clone())?;
``` [3](#0-2) 

The `?` operator propagates `Err(...)` values but **does not catch panics**. A panic in `calculate_dao_field` propagates through `generate_block_with_template` unimpeded, crashing the connection handler task.

---

### Impact Explanation

When the `IntegrationTestRpc` module is enabled (via `rpc.modules` in `ckb.toml`), any unauthenticated RPC caller can send a `calculate_dao_field` or `generate_block_with_template` request with an arbitrary `parent_hash`. The node's RPC connection handler task panics and is aborted by the tokio runtime. The attacker can repeat this indefinitely, causing a sustained DoS against the RPC service for any caller attempting to use these endpoints. Nodes running in development, staging, or CI environments with this module enabled are directly affected.

---

### Likelihood Explanation

The `IntegrationTestRpc` module must be explicitly enabled in node configuration. Nodes that enable it — common in development, integration testing, and some operator tooling setups — are fully exposed. The attack requires no authentication, no privileged key, and no special knowledge: sending a single JSON-RPC request with a random 32-byte hex string as `parent_hash` is sufficient to trigger the panic. The attack is trivially scriptable and repeatable.

---

### Recommendation

Replace both `.expect()` calls with proper `Result`-returning error handling:

```rust
// Line 722–724: replace
let parent_header = snapshot
    .get_block_header(&(&block_template.parent_hash).into())
    .ok_or_else(|| RPCError::invalid_params(
        format!("parent block not found: {:?}", block_template.parent_hash)
    ))?;

// Line 756–759: replace
let dao_field = DaoCalculator::new(consensus, &snapshot.borrow_as_data_loader())
    .dao_field(rtxs.iter(), &parent_header)
    .map_err(|err| RPCError::custom_with_error(RPCError::DaoError, err))?;
Ok(dao_field.into())
```

Additionally, add input validation at the RPC boundary to reject unknown `parent_hash` values before entering internal logic, consistent with the external report's recommendation to validate inputs before passing them to internal function calls.

---

### Proof of Concept

Send the following JSON-RPC request to a node with `IntegrationTestRpc` enabled:

```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "calculate_dao_field",
  "params": [{
    "parent_hash": "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "compact_target": "0x1e083126",
    "current_time": "0x174c45e17a3",
    "cycles_limit": "0xd09dc300",
    "bytes_limit": "0x91c08",
    "uncles_count_limit": "0x2",
    "uncles": [],
    "transactions": [],
    "proposals": [],
    "cellbase": {"cycles": null, "data": {"cell_deps": [], "header_deps": [], "inputs": [{"previous_output": {"index": "0xffffffff", "tx_hash": "0x0000000000000000000000000000000000000000000000000000000000000000"}, "since": "0x1"}], "outputs": [], "outputs_data": [], "version": "0x0", "witnesses": []}, "hash": "0x0000000000000000000000000000000000000000000000000000000000000000"},
    "dao": "0x0000000000000000000000000000000000000000000000000000000000000000",
    "epoch": "0x1",
    "number": "0x1",
    "version": "0x0",
    "work_id": "0x0"
  }]
}
```

The `parent_hash` `0xdeadbeef...` does not exist in the database. The call to `snapshot.get_block_header(...)` returns `None`. The `.expect("parent header should be stored")` at line 724 panics. The RPC connection handler task is aborted. The server returns a connection error to the caller. Repeating this request causes repeated handler crashes. [4](#0-3)

### Citations

**File:** rpc/src/module/test.rs (L710-716)
```rust
    fn generate_block_with_template(&self, block_template: BlockTemplate) -> Result<H256> {
        let dao_field = self.calculate_dao_field(block_template.clone())?;

        let mut update_dao_template = block_template;
        update_dao_template.dao = dao_field;
        let block = update_dao_template.into();
        self.process_and_announce_block(block)
```

**File:** rpc/src/module/test.rs (L719-761)
```rust
    fn calculate_dao_field(&self, block_template: BlockTemplate) -> Result<Byte32> {
        let snapshot: &Snapshot = &self.shared.snapshot();
        let consensus = snapshot.consensus();
        let parent_header = snapshot
            .get_block_header(&(&block_template.parent_hash).into())
            .expect("parent header should be stored");
        let mut seen_inputs = HashSet::new();

        let txs: Vec<_> = packed::Block::from(block_template)
            .transactions()
            .into_iter()
            .map(|tx| tx.into_view())
            .collect();

        let transactions_provider = TransactionsProvider::new(txs.as_slice().iter());
        let overlay_cell_provider = OverlayCellProvider::new(&transactions_provider, snapshot);
        let rtxs = txs
            .iter()
            .map(|tx| {
                resolve_transaction(
                    tx.clone(),
                    &mut seen_inputs,
                    &overlay_cell_provider,
                    snapshot,
                )
                .map_err(|err| {
                    error!(
                        "Resolve transactions error when generating block \
                         with block template, error: {:?}",
                        err
                    );
                    RPCError::invalid_params(err.to_string())
                })
            })
            .collect::<Result<Vec<ResolvedTransaction>>>()?;

        Ok(
            DaoCalculator::new(consensus, &snapshot.borrow_as_data_loader())
                .dao_field(rtxs.iter(), &parent_header)
                .expect("dao calculation should be OK")
                .into(),
        )
    }
```
