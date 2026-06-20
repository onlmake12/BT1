### Title
`submit_block` RPC Accepts Any `work_id` Without Validation Against Current Block Template — (`File: rpc/src/module/miner.rs`)

### Summary

The `submit_block` RPC handler in `MinerRpcImpl` accepts a `work_id: String` parameter that is documented as a required correlation token — "The miner must submit the new assembled and resolved block using the same work ID" — but the implementation never validates this value against the current block assembler's `work_id`. Any RPC caller can submit any valid PoW block with any arbitrary `work_id` string (including an empty string `""`), and the node will process and broadcast it without checking whether the submitter ever obtained a block template.

### Finding Description

The `BlockAssembler` maintains a monotonically incrementing `work_id: Arc<AtomicU64>` counter. Each call to `update_full`, `update_blank`, `update_uncles`, `update_proposals`, or `update_transactions` increments this counter and embeds the new value in the returned `BlockTemplate`. The `get_block_template` RPC returns this `work_id` to the caller, and the documentation states the miner must use the same `work_id` when calling `submit_block`.

However, in `MinerRpcImpl::submit_block`, the received `work_id: String` is only used in log/error messages. It is never parsed, never compared to the current template's `work_id`, and never checked against any stored state:

```rust
fn submit_block(&self, work_id: String, block: Block) -> Result<H256> {
    // work_id is only used in debug/error logging below
    debug!("start to submit block, work_id = {}, block = ...", work_id, ...);
    // Verify header (PoW, timestamp, etc.) — no work_id check
    HeaderVerifier::new(snapshot, consensus).verify(&header)...;
    // Verify and insert block — no work_id check
    let is_new = self.chain.blocking_process_block(Arc::clone(&block))...;
    // Broadcast if new — no work_id check
    ...
    Ok(header.hash().into())
}
```

The integration tests confirm this: they routinely call `submit_block("".to_owned(), ...)` with an empty string and the node accepts it without error.

### Impact Explanation

The `work_id` parameter is the only mechanism intended to correlate a block submission with a prior `get_block_template` call. Since it is never validated, any process that can reach the RPC endpoint can submit a valid PoW block without having obtained a template from this node. The node will process and broadcast the block to the network as if it were a legitimate miner submission. The practical security impact is **informational**: PoW is still required (the real authorization barrier), and the RPC is bound to `127.0.0.1:8114` by default. The `work_id` was never a cryptographic secret — it is a sequential integer — so bypassing it does not enable block forgery. The violated invariant is the documented contract, not a consensus rule.

### Likelihood Explanation

Any local process (or remote caller if `rpc.listen_address` is changed from the default localhost) can trivially call `submit_block` with any `work_id` string. No privilege, key, or secret is required. The attack surface is the RPC endpoint, which is reachable by any local RPC caller by default.

### Recommendation

Either:
1. Enforce the `work_id` contract by storing the most recently issued `work_id` in `BlockAssembler` and rejecting submissions whose `work_id` does not match, or
2. Remove the `work_id` parameter from the RPC signature and documentation if it is not intended to be a security control, to avoid creating a false expectation of authorization.

### Proof of Concept

The integration test suite itself demonstrates the bypass — blocks are submitted with `work_id = ""` throughout:

```rust
// test/src/node.rs:393-399
pub fn submit_block(&self, block: &BlockView) -> Byte32 {
    let hash = self
        .rpc_client()
        .submit_block("".to_owned(), block.data().into()) // empty work_id accepted
        .unwrap();
    ...
}
```

A remote attacker (or any local process) can replicate this:
```json
{
  "jsonrpc": "2.0",
  "method": "submit_block",
  "params": ["arbitrary_string", { /* valid PoW block */ }],
  "id": 1
}
```
The node processes the block identically regardless of the `work_id` value.

---

**Root cause:** [1](#0-0) 

**`work_id` is only logged, never validated:** [2](#0-1) 

**`BlockAssembler` maintains the authoritative `work_id` counter that is never consulted during submission:** [3](#0-2) 

**`work_id` is documented as a required correlation token in the JSON-RPC type:** [4](#0-3) 

**Test code confirms empty `work_id` is accepted without error:** [5](#0-4)

### Citations

**File:** rpc/src/module/miner.rs (L260-277)
```rust
    fn submit_block(&self, work_id: String, block: Block) -> Result<H256> {
        let block: packed::Block = block.into();
        let block: Arc<core::BlockView> = Arc::new(block.into_view());
        let header = block.header();
        debug!(
            "start to submit block, work_id = {}, block = #{}({})",
            work_id,
            block.number(),
            block.hash()
        );

        let snapshot: &Snapshot = &self.shared.snapshot();
        let consensus = snapshot.consensus();

        // Verify header
        HeaderVerifier::new(snapshot, consensus)
            .verify(&header)
            .map_err(|err| handle_submit_error(&work_id, &err))?;
```

**File:** tx-pool/src/block_assembler/mod.rs (L103-108)
```rust
pub struct BlockAssembler {
    pub(crate) config: Arc<BlockAssemblerConfig>,
    pub(crate) work_id: Arc<AtomicU64>,
    pub(crate) candidate_uncles: Arc<Mutex<CandidateUncles>>,
    pub(crate) current: Arc<Mutex<CurrentTemplate>>,
    pub(crate) poster: Arc<Client<HttpConnector, Full<bytes::Bytes>>>,
```

**File:** util/jsonrpc-types/src/block_template.rs (L76-77)
```rust
    /// Work ID. The miner must submit the new assembled and resolved block using the same work ID.
    pub work_id: Uint64,
```

**File:** test/src/node.rs (L393-399)
```rust
    pub fn submit_block(&self, block: &BlockView) -> Byte32 {
        let hash = self
            .rpc_client()
            .submit_block("".to_owned(), block.data().into())
            .unwrap();
        self.wait_for_tx_pool();
        hash
```
