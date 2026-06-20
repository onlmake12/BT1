### Title
`process_block_without_verify` Always Returns `Ok(...)` — Failure Status Is Never Propagated to RPC Caller - (File: `rpc/src/module/test.rs`)

### Summary

`IntegrationTestRpcImpl::process_block_without_verify` swallows the block-processing error and returns `Ok(None)` to the caller instead of `Err(...)`. This is the direct CKB analog of the reported pattern: a function that should signal failure always signals success, breaking the invariant that the return value reflects the actual processing outcome.

### Finding Description

In `rpc/src/module/test.rs`, the `process_block_without_verify` RPC handler calls `blocking_process_block_with_switch` with `Switch::DISABLE_ALL` and inspects the result:

```rust
fn process_block_without_verify(&self, data: Block, broadcast: bool) -> Result<Option<H256>> {
    let block: Arc<BlockView> = Arc::new(block.into_view());
    let ret = self
        .chain
        .blocking_process_block_with_switch(Arc::clone(&block), Switch::DISABLE_ALL);
    // ...broadcast logic...
    if ret.is_ok() {
        Ok(Some(block.hash().into()))
    } else {
        error!("process_block_without_verify error: {:?}", ret);
        Ok(None)   // ← error is swallowed; caller always receives Ok(...)
    }
}
``` [1](#0-0) 

The function signature is `Result<Option<H256>>`, which gives the caller three possible outcomes:

| Intended meaning | Expected return |
|---|---|
| Block newly processed | `Ok(Some(hash))` |
| Block already stored (not new) | `Ok(None)` |
| Block processing failed | `Err(...)` |

Because the error branch returns `Ok(None)` instead of `Err(...)`, the third case is collapsed into the second. The caller receives `Ok(None)` for both "block already stored" and "block processing failed", making the two indistinguishable. The error is only written to the node log; it is never surfaced to the RPC caller.

Compare this with `MinerRpcImpl::submit_block`, which correctly propagates errors:

```rust
let is_new = self
    .chain
    .blocking_process_block(Arc::clone(&block))
    .map_err(|err| handle_submit_error(&work_id, &err))?;  // propagates Err
``` [2](#0-1) 

The `blocking_process_block_with_switch` path used by `process_block_without_verify` goes through the same `ConsumeUnverifiedBlockProcessor::consume_unverified_blocks` pipeline, which correctly produces `VerifyResult = Result<bool, Error>` and fires the callback with the real outcome: [3](#0-2) 

The callback result is therefore available; it is simply discarded at the RPC layer.

### Impact Explanation

Any automated tool or script that calls `process_block_without_verify` and inspects the return value to decide whether the block was accepted will misinterpret a processing failure as "block already stored". This breaks the status-tracking invariant: the caller cannot distinguish a rejected block from a duplicate one. In a test harness or chain-import workflow that relies on this RPC to drive subsequent steps (e.g., building on top of the submitted block), silently swallowed failures can cause the workflow to proceed on an incorrect chain state without any error signal.

### Likelihood Explanation

The `IntegrationTestRpc` module is a documented, production-compiled RPC endpoint reachable by any local RPC user who has the module enabled. The `process_block_without_verify` method is explicitly listed in the RPC README and is callable by a supported local RPC user — one of the attacker roles listed in scope. [4](#0-3) 

### Recommendation

Propagate the error to the caller instead of returning `Ok(None)`:

```rust
if ret.is_ok() {
    Ok(Some(block.hash().into()))
} else {
    error!("process_block_without_verify error: {:?}", ret);
    Err(RPCError::custom_with_error(RPCError::Invalid, ret.unwrap_err()))
}
```

This mirrors the pattern used in `submit_block` and ensures the caller can distinguish a processing failure from a duplicate-block response.

### Proof of Concept

1. Enable the `IntegrationTestRpc` module on a dev node.
2. Submit a block whose parent is unknown (guaranteed to fail chain insertion).
3. Call `process_block_without_verify` with that block.
4. Observe the RPC response: `{"result": null}` (`Ok(None)`) — identical to the "already stored" response — with no error code, even though the node log shows `process_block_without_verify error: ...`.
5. A caller checking `result == null` cannot tell whether the block was rejected or was a duplicate. [5](#0-4)

### Citations

**File:** rpc/src/module/test.rs (L31-42)
```rust
/// RPC for Integration Test.
#[rpc(openrpc)]
#[async_trait]
pub trait IntegrationTestRpc {
    /// process block without any block verification.
    ///
    /// ## Params
    ///
    /// * `data` - block data(in binary).
    ///
    /// * `broadcast` - true to enable broadcast(relay) the block to other peers.
    ///
```

**File:** rpc/src/module/test.rs (L606-627)
```rust
    fn process_block_without_verify(&self, data: Block, broadcast: bool) -> Result<Option<H256>> {
        let block: packed::Block = data.into();
        let block: Arc<BlockView> = Arc::new(block.into_view());
        let ret = self
            .chain
            .blocking_process_block_with_switch(Arc::clone(&block), Switch::DISABLE_ALL);
        if broadcast {
            let content = packed::CompactBlock::build_from_block(&block, &HashSet::new());
            let message = packed::RelayMessage::new_builder().set(content).build();
            self.network_controller.quick_broadcast_with_handle(
                SupportProtocols::RelayV3.protocol_id(),
                message.as_bytes(),
                self.shared.async_handle(),
            );
        }
        if ret.is_ok() {
            Ok(Some(block.hash().into()))
        } else {
            error!("process_block_without_verify error: {:?}", ret);
            Ok(None)
        }
    }
```

**File:** rpc/src/module/miner.rs (L295-298)
```rust
        let is_new = self
            .chain
            .blocking_process_block(Arc::clone(&block))
            .map_err(|err| handle_submit_error(&work_id, &err))?;
```

**File:** chain/src/verify.rs (L139-197)
```rust
        let verify_result = self.verify_block(&block, &parent_header, switch);
        match &verify_result {
            Ok(_) => {
                let log_now = std::time::Instant::now();
                self.shared.remove_block_status(&block_hash);
                let log_elapsed_remove_block_status = log_now.elapsed();
                self.shared.remove_header_view(&block_hash);
                debug!(
                    "block {} remove_block_status cost: {:?}, and header_view cost: {:?}",
                    block_hash,
                    log_elapsed_remove_block_status,
                    log_now.elapsed()
                );
            }
            Err(err) => {
                error!("verify block {} failed: {}", block_hash, err);

                let tip = self
                    .shared
                    .store()
                    .get_tip_header()
                    .expect("tip_header must exist");
                let tip_ext = self
                    .shared
                    .store()
                    .get_block_ext(&tip.hash())
                    .expect("tip header's ext must exist");

                self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                    tip.number(),
                    tip.hash(),
                    tip_ext.total_difficulty,
                ));

                self.delete_unverified_block(&block);

                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
                    error!("internal db error, remove block status: {}", block_hash);
                    self.shared.remove_block_status(&block_hash);
                }

                error!(
                    "set_unverified tip to {}-{}, because verify {} failed: {}",
                    tip.number(),
                    tip.hash(),
                    block_hash,
                    err
                );
            }
        }

        self.is_pending_verify.remove(&block_hash);

        if let Some(callback) = verify_callback {
            callback(verify_result);
        }
```
