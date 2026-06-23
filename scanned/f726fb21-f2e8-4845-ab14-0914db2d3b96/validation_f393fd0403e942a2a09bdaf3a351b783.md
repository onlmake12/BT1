### Title
`send_transaction` RPC Accepts Transactions During IBD Without State Guard - (File: `rpc/src/module/pool.rs`)

### Summary
The `send_transaction` RPC handler in CKB does not check whether the node is in Initial Block Download (IBD) mode before accepting and submitting a transaction to the tx-pool. This is the direct analog of the reported `cancelInv()` missing `whenNotPaused()` modifier: a state-sensitive operation proceeds without verifying the system is in the correct operational state.

### Finding Description
CKB's IBD mode is the protocol-level "paused/restricted" state. During IBD, the node is synchronizing its chain from a single trusted peer and explicitly blocks most P2P operations. The relay protocol enforces this:

```rust
// sync/src/relayer/mod.rs:816-818
if self.shared.active_chain().is_initial_block_download() {
    return;
}
```

The `GetHeadersProcess` also enforces it:

```rust
// sync/src/synchronizer/get_headers_process.rs:53-66
if active_chain.is_initial_block_download() {
    info!("Ignoring getheaders from peer=...");
    self.send_in_ibd();
    return Status::ignored();
}
```

However, the `send_transaction` RPC handler in `rpc/src/module/pool.rs` performs **no IBD check**:

```rust
fn send_transaction(
    &self,
    tx: Transaction,
    outputs_validator: Option<OutputsValidator>,
) -> Result<H256> {
    let tx: packed::Transaction = tx.into();
    let tx: core::TransactionView = tx.into_view();

    self.check_output_validator(outputs_validator, &tx)?;

    let tx_pool = self.shared.tx_pool_controller();
    let submit_tx = tx_pool.submit_local_tx(tx.clone());
    // No is_initial_block_download() check here
    ...
}
```

The `Shared` struct has `is_initial_block_download()` available and is already held by `PoolRpcImpl`. The check is simply absent.

The same missing guard applies to `test_tx_pool_accept` and `remove_transaction` in the same file.

### Impact Explanation
During IBD, the node's chain snapshot is stale and incomplete — it has not yet verified the full chain. Transactions submitted via `send_transaction` during IBD are resolved and verified against this stale snapshot. This means:

1. **Transactions referencing cells that appear live in the stale snapshot but are actually spent** will be accepted into the pool, only to be evicted or cause inconsistency once IBD completes and the snapshot is updated.
2. **An attacker or misconfigured client** can flood the tx-pool with transactions during IBD, consuming memory and CPU verification resources at a time when the node is already under maximum load (syncing blocks). This degrades IBD performance.
3. The tx-pool's own IBD state tracking (`update_ibd_state`) is only used for the fee estimator — the pool itself does not reject submissions during IBD, so the missing RPC guard is the only enforcement point.

### Likelihood Explanation
Any RPC caller (local CLI user, wallet, dApp) can call `send_transaction` at any time, including during IBD. A node freshly started on mainnet will be in IBD for an extended period. The RPC endpoint is reachable by any configured RPC client without authentication by default. This is a realistic and easily triggered condition.

### Recommendation
Add an IBD state check at the top of `send_transaction` (and `test_tx_pool_accept`) in `rpc/src/module/pool.rs`, analogous to the guard already used in the relay and sync handlers:

```rust
fn send_transaction(&self, tx: Transaction, outputs_validator: Option<OutputsValidator>) -> Result<H256> {
    if self.shared.is_initial_block_download() {
        return Err(RPCError::custom(RPCError::Invalid, "node is in initial block download, transaction submission is not allowed".to_string()));
    }
    // ... rest of the function
}
```

### Proof of Concept

1. Start a fresh CKB node on mainnet (or a network with many blocks). The node enters IBD immediately.
2. Confirm IBD state: `curl -X POST ... -d '{"method":"get_blockchain_info",...}'` → `"is_initial_block_download": true`
3. Submit a transaction via RPC: `curl -X POST ... -d '{"method":"send_transaction","params":[<tx>,"passthrough"]}'`
4. The call succeeds (returns a tx hash) despite the node being in IBD, bypassing the intended restriction that the node should not process transactions while its chain state is incomplete.

**Root cause location:** [1](#0-0) 

**IBD guard present in relay (for comparison):** [2](#0-1) 

**IBD guard present in sync GetHeaders (for comparison):** [3](#0-2) 

**`is_initial_block_download()` is accessible on `Shared` (used in stats RPC):** [4](#0-3) 

**IBD state definition:** [5](#0-4)

### Citations

**File:** rpc/src/module/pool.rs (L612-635)
```rust
    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();
        let submit_tx = tx_pool.submit_local_tx(tx.clone());

        if let Err(e) = submit_tx {
            error!("Send submit_tx request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        }

        let tx_hash = tx.hash();
        match submit_tx.unwrap() {
            Ok(_) => Ok(tx_hash.into()),
            Err(reject) => Err(RPCError::from_submit_transaction_reject(&reject)),
        }
    }
```

**File:** sync/src/relayer/mod.rs (L815-818)
```rust
        // If self is in the IBD state, don't process any relayer message.
        if self.shared.active_chain().is_initial_block_download() {
            return;
        }
```

**File:** sync/src/synchronizer/get_headers_process.rs (L53-66)
```rust
        if active_chain.is_initial_block_download() {
            info!(
                "Ignoring getheaders from peer={} because the node is in initial block download stage.",
                self.peer
            );
            self.send_in_ibd();
            let shared = self.synchronizer.shared();
            if let Some(flag) = shared.state().peers().get_flag(self.peer)
                && (flag.is_outbound || flag.is_whitelist || flag.is_protect)
            {
                shared.insert_peer_unknown_header_list(self.peer, block_locator_hashes);
            };
            return Status::ignored();
        }
```

**File:** rpc/src/module/stats.rs (L131-131)
```rust
        let is_initial_block_download = self.shared.is_initial_block_download();
```

**File:** sync/src/types/mod.rs (L1979-1999)
```rust
/// The `IBDState` enum represents whether the node is currently in the IBD process (`In`) or has
/// completed it (`Out`).
#[derive(Clone, Copy, Debug)]
pub enum IBDState {
    In,
    Out,
}

impl From<bool> for IBDState {
    fn from(src: bool) -> Self {
        if src { IBDState::In } else { IBDState::Out }
    }
}

impl From<IBDState> for bool {
    fn from(s: IBDState) -> bool {
        match s {
            IBDState::In => true,
            IBDState::Out => false,
        }
    }
```
