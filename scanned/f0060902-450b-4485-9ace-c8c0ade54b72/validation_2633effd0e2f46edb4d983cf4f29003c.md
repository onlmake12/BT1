All key code paths are confirmed. Let me verify one more detail about the `resumeble_process_tx` routing logic.

Audit Report

## Title
`declared_cycles` Used as Script Execution Cap Causes Permanent Relay Suppression via Malformed-Tx Classification - (File: tx-pool/src/process.rs)

## Summary
In `tx-pool/src/process.rs`, `_process_tx` uses the peer-supplied `declared_cycles` as the hard execution-cycle cap passed to `verify_rtx`. When `declared_cycles` is below the transaction's actual cycle cost, the script verifier terminates early with `ScriptError::ExceededMaximumCycles`, which is classified as a malformed-transaction error. This causes `is_allowed_relay` to return `false`, so `TxVerificationResult::Reject` is never sent to the relayer, and the transaction hash — already marked as known before pool submission — is never removed from the known-tx filter. Any unprivileged peer can permanently suppress relay of a valid transaction on a target node for the filter's TTL by relaying it with `declared_cycles = 1`.

## Finding Description

**Root cause — `tx-pool/src/process.rs`, `_process_tx` (line 720):**

`declared_cycles` is used as the execution cap passed to `verify_rtx`:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
// ...
let verified_ret = verify_rtx(..., max_cycles, command_rx).await;
``` [1](#0-0) 

`verify_rtx` passes `max_cycles` directly to `ContextualTransactionVerifier::verify` or `verify_with_pause`, which enforces it as a hard cap: [2](#0-1) 

When `declared_cycles < actual_cycles`, the VM hits the cap and returns `VMInternalError::CyclesExceeded`, which is mapped to `ScriptError::ExceededMaximumCycles`: [3](#0-2) 

This surfaces as `Reject::Verification(ErrorKind::Script)`. `is_malformed_from_verification` classifies all `ErrorKind::Script` errors (except those containing `ARGV_TOO_LONG_TEXT`) as malformed — `ExceededMaximumCycles` does not contain that string: [4](#0-3) 

`is_allowed_relay` returns `false` for malformed errors. Only `DeclaredWrongCycles` gets the explicit carve-out: [5](#0-4) 

In `after_process`, when `is_allowed_relay()` is false, `send_result_to_relayer(TxVerificationResult::Reject)` is never called: [6](#0-5) 

The tx hash was already marked as known **before** pool submission in `TransactionsProcess::execute`: [7](#0-6) 

`remove_from_known_txs` is only triggered by `TxVerificationResult::Reject`, which is never sent: [8](#0-7) 

**Asymmetry with `_test_accept_tx`:** That function correctly uses `self.consensus.max_block_cycles()` as the cap, never `declared_cycles`: [9](#0-8) 

**Contrast with `declared > actual` path:** When `declared_cycles > actual_cycles`, the script completes, `verified.cycles` is produced, the exact-match check fires, and `Reject::DeclaredWrongCycles` is returned. `is_allowed_relay` returns `true` for `DeclaredWrongCycles`, so `TxVerificationResult::Reject` is sent and the hash is removed from the filter. The `DeclaredWrongCyclesAndRelayAgain` integration test confirms this recovery path works correctly. [10](#0-9) 

**No lower-bound guard:** The only existing check in `TransactionsProcess::execute` rejects `declared_cycles > max_block_cycles`. There is no check preventing `declared_cycles = 1`: [11](#0-10) 

## Impact Explanation

An unprivileged peer can suppress relay of any valid transaction on a target node for the duration of the known-tx filter's TTL. The transaction is marked as known, rejected as malformed (script cycles exceeded), and never re-requestable from other peers. If the attacker targets multiple nodes simultaneously using multiple IPs or Sybil connections, valid transactions can be prevented from propagating across a significant portion of the network, delaying or blocking confirmation. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**. The cost to the attacker is a single P2P relay message per target node; the attacker is banned after the attempt but the suppression persists.

## Likelihood Explanation

The attack requires only a standard `RelayTransactions` P2P message with the `cycles` field set to any value below the transaction's actual cost (e.g., `1`). No privileged access, no key material, and no majority hash power is required. The target transaction hash is observable from the public mempool or pending proposals. The only existing guard checks for `declared_cycles > max_block_cycles`; there is no lower-bound check. The attack is trivially repeatable across multiple nodes using multiple IP addresses.

## Recommendation

Use `max_block_cycles` (or `max_tx_verify_cycles`) as the execution cap in `_process_tx`, independent of `declared_cycles`. Reserve `declared_cycles` solely for the post-verification exact-match check, matching the behavior of `_test_accept_tx`:

```rust
// Use consensus limit for execution, not the peer-declared value
let max_cycles = self.consensus.max_block_cycles();

let verified_ret = verify_rtx(..., max_cycles, command_rx).await;
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

// Only now compare against declared_cycles
if let Some(declared) = declared_cycles
    && declared != verified.cycles
{
    return Some((Err(Reject::DeclaredWrongCycles(declared, verified.cycles)), snapshot));
}
```

This ensures that a peer declaring too-low cycles receives `DeclaredWrongCycles` (re-relay allowed, hash removed from filter) rather than a script error (re-relay suppressed, hash permanently stuck in filter).

## Proof of Concept

1. Node A has `max_block_cycles = 3_500_000_000`.
2. A valid transaction `T` uses the `always_success` script requiring `537` cycles.
3. Attacker peer connects to Node A and sends `RelayTransactions` with `T` and `cycles = 1`.
4. `TransactionsProcess::execute`: `1 <= max_block_cycles` → no ban; tx hash marked as known; `submit_remote_tx(T, declared_cycles=1, peer)` called.
5. `_process_tx`: `max_cycles = 1`; `verify_rtx` runs script with cap `1`; script aborts with `ExceededMaximumCycles(1)` → `Reject::Verification(ErrorKind::Script)`.
6. `after_process`: `is_malformed_tx() = true` → attacker peer banned; `is_allowed_relay() = false` → `TxVerificationResult::Reject` NOT sent.
7. `T`'s hash remains in Node A's known-tx filter; Node A will not request `T` from any other peer for the filter's TTL.
8. Legitimate peers relaying `T` with correct cycles `537` are ignored by Node A (tx already "known").

To reproduce: adapt the existing `DeclaredWrongCyclesAndRelayAgain` integration test by changing the attacker's declared cycles from `ALWAYS_SUCCESS_SCRIPT_CYCLE + 1` to `1`. The test should show that after the attacker is banned, Node A still does **not** accept the transaction from Node 1 with correct cycles — confirming the hash is stuck in the filter and `remove_from_known_txs` was never called. [12](#0-11)

### Citations

**File:** tx-pool/src/process.rs (L514-521)
```rust
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
                        }
                        if reject.is_allowed_relay() {
                            self.send_result_to_relayer(TxVerificationResult::Reject {
                                tx_hash: tx_hash.clone(),
                            });
                        }
```

**File:** tx-pool/src/process.rs (L720-732)
```rust
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;
```

**File:** tx-pool/src/process.rs (L787-787)
```rust
        let max_cycles = self.consensus.max_block_cycles();
```

**File:** tx-pool/src/util.rs (L101-115)
```rust
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
```

**File:** script/src/verify.rs (L566-569)
```rust
    fn map_vm_internal_error(&self, error: VMInternalError, max_cycles: Cycle) -> ScriptError {
        match error {
            VMInternalError::CyclesExceeded => ScriptError::ExceededMaximumCycles(max_cycles),
            VMInternalError::External(reason) if reason.eq("stopped") => ScriptError::Interrupts,
```

**File:** util/types/src/core/tx_pool.rs (L69-85)
```rust
fn is_malformed_from_verification(error: &Error) -> bool {
    match error.kind() {
        ErrorKind::Transaction => error
            .downcast_ref::<TransactionError>()
            .expect("error kind checked")
            .is_malformed_tx(),
        ErrorKind::Script => !format!("{}", error).contains(ARGV_TOO_LONG_TEXT),
        ErrorKind::Internal => {
            error
                .downcast_ref::<InternalError>()
                .expect("error kind checked")
                .kind()
                == InternalErrorKind::CapacityOverflow
        }
        _ => false,
    }
}
```

**File:** util/types/src/core/tx_pool.rs (L110-113)
```rust
    pub fn is_allowed_relay(&self) -> bool {
        matches!(self, Reject::DeclaredWrongCycles(..))
            || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
    }
```

**File:** sync/src/relayer/transactions_process.rs (L63-74)
```rust
        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }
```

**File:** sync/src/relayer/transactions_process.rs (L76-76)
```rust
        shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));
```

**File:** sync/src/relayer/mod.rs (L673-675)
```rust
                    TxVerificationResult::Reject { tx_hash } => {
                        self.shared.state().remove_from_known_txs(&tx_hash);
                    }
```

**File:** test/src/specs/tx_pool/declared_wrong_cycles.rs (L36-67)
```rust
pub struct DeclaredWrongCyclesChunk;

impl Spec for DeclaredWrongCyclesChunk {
    crate::setup!(num_nodes: 1);

    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &mut nodes[0];
        node0.mine_until_out_bootstrap_period();

        let mut net = Net::new(
            self.name(),
            node0.consensus(),
            vec![SupportProtocols::RelayV3],
        );
        net.connect(node0);

        let tx = node0.new_transaction_spend_tip_cellbase();

        relay_tx(&net, node0, tx, ALWAYS_SUCCESS_SCRIPT_CYCLE + 1);

        let result = wait_until(5, || {
            let tx_pool_info = node0.get_tip_tx_pool_info();
            tx_pool_info.orphan.value() == 0 && tx_pool_info.pending.value() == 0
        });
        assert!(result, "Declared wrong cycles should be rejected");
    }

    fn modify_app_config(&self, config: &mut ckb_app_config::CKBAppConfig) {
        config.network.connect_outbound_interval_secs = 0;
        config.tx_pool.max_tx_verify_cycles = 500; // ALWAYS_SUCCESS_SCRIPT_CYCLE: u64 = 537
    }
}
```

**File:** test/src/specs/tx_pool/declared_wrong_cycles.rs (L69-117)
```rust
pub struct DeclaredWrongCyclesAndRelayAgain;

impl Spec for DeclaredWrongCyclesAndRelayAgain {
    crate::setup!(num_nodes: 3);

    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &nodes[0];
        let node1 = &nodes[1];
        let node2 = &nodes[2];
        node0.mine_until_out_bootstrap_period();
        out_ibd_mode(nodes);

        let mut net = Net::new(
            self.name(),
            node0.consensus(),
            vec![SupportProtocols::RelayV3],
        );

        let tx = node0.new_transaction_spend_tip_cellbase();
        // relay tx to node0 with wrong cycles
        net.connect(node0);
        relay_tx(&net, node0, tx.clone(), ALWAYS_SUCCESS_SCRIPT_CYCLE + 1);
        let ret = wait_until(10, || node0.rpc_client().get_peers().is_empty());
        assert!(
            ret,
            "The address of net should be removed from node0's peers",
        );
        // connect node0 and node2, make sure node0's relay tx hash processing is working
        node0.rpc_client().clear_banned_addresses();
        node0.connect(node2);
        // removing invalid tx hash from node0's known tx filer is async, wait 5 seconds to make sure it's removed
        sleep(5);

        // connect node0 with node1, tx will be relayed from node1 to node0
        node0.connect(node1);

        // relay tx to node1 with correct cycles
        net.connect(node1);
        relay_tx(&net, node1, tx, ALWAYS_SUCCESS_SCRIPT_CYCLE);

        let result = wait_until(5, || {
            let tx_pool_info = node0.get_tip_tx_pool_info();
            tx_pool_info.orphan.value() == 0 && tx_pool_info.pending.value() == 1
        });
        assert!(
            result,
            "Tx with wrong cycles should be relayed again with correct cycle"
        );
    }
```
