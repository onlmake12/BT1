### Title
Peer-Controlled `declared_cycles` Used as Verification Cap Enables Transaction Relay DoS - (File: `tx-pool/src/process.rs`)

### Summary

A remote peer relaying a transaction via the `RelayTransactions` P2P message supplies a `declared_cycles` field that CKB uses directly as the `max_cycles` upper bound for CKB-VM script verification. When a malicious peer deliberately under-declares cycles for a valid transaction, the verification fails with `ExceededMaximumCycles` (a `Reject::Verification` / Script error). This error is classified as `is_malformed_tx() = true` but `is_allowed_relay() = false`, so the transaction hash is never removed from the node's known-tx filter. Because the hash was already inserted into the filter before verification, honest peers cannot relay the same transaction to the victim node, effectively blocking it from the tx-pool.

### Finding Description

**Entry point — `TransactionsProcess::execute()`** (`sync/src/relayer/transactions_process.rs`):

A peer sends a `RelayTransactions` message. The handler extracts the peer-supplied `declared_cycles` and marks the transaction hash as **known** before any verification occurs:

```
shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));
``` [1](#0-0) 

The only pre-filter is a ban if `declared_cycles > max_block_cycles`. A value of `declared_cycles = 1` (or any value below the actual script cost) passes this check. [2](#0-1) 

**Root cause — `_process_tx()`** (`tx-pool/src/process.rs`):

The peer-supplied `declared_cycles` is used verbatim as `max_cycles` for the CKB-VM verifier:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
``` [3](#0-2) 

`verify_rtx` is then called with this attacker-controlled cap: [4](#0-3) 

If `declared_cycles < actual_script_cycles`, the VM halts with `ExceededMaximumCycles`, producing `Reject::Verification(Script error)`.

**Misclassification — `Reject` trait** (`util/types/src/core/tx_pool.rs`):

`is_malformed_from_verification` returns `true` for any `ErrorKind::Script` error that does not contain `ARGV_TOO_LONG_TEXT`: [5](#0-4) 

Therefore `is_malformed_tx()` returns `true` for this error, and `is_allowed_relay()` returns `false`: [6](#0-5) 

**Consequence — `after_process()`** (`tx-pool/src/process.rs`):

Because `is_allowed_relay()` is `false`, `TxVerificationResult::Reject` is **never sent** to the relayer. The tx hash is not removed from the known-tx filter. The peer is banned, but the filter entry persists. [7](#0-6) 

Contrast with `Reject::DeclaredWrongCycles` (over-declaration): that variant is explicitly listed in `is_allowed_relay()` so the tx hash IS cleared and re-relay is possible: [8](#0-7) 

### Impact Explanation

An unprivileged remote peer can permanently suppress a specific valid transaction from a victim node's tx-pool for the lifetime of the known-tx filter entry. By repeatedly connecting (after ban expiry or via different IPs) and re-sending the same transaction with `declared_cycles = 1`, the attacker keeps the tx hash in the filter indefinitely. Honest peers that later try to relay the same transaction are silently dropped by the filter check:

```rust
!tx_filter.contains(&tx.hash())
``` [9](#0-8) 

This prevents targeted transactions from ever entering the victim node's pending pool, blocking their eventual confirmation — a targeted transaction-relay DoS.

### Likelihood Explanation

The attack requires only a standard P2P connection. No privileged access, no key material, and no majority hashpower is needed. The attacker only needs to know the hash of a target transaction (observable from any other node's mempool broadcast) and send it with `declared_cycles = 1`. The ban is per-IP and can be rotated. The `tx_filter.remove_expired()` call does eventually expire entries, but the attacker can re-inject before expiry. [10](#0-9) 

### Recommendation

1. **Do not use `declared_cycles` as `max_cycles` for verification.** Use `min(declared_cycles, consensus.max_block_cycles())` only as a hint for queue prioritization. Always verify with `consensus.max_block_cycles()` (or `max_tx_verify_cycles`) as the actual cap.
2. **Mark the tx hash as known only after successful verification**, or remove it from the filter on any non-`DeclaredWrongCycles` failure so honest peers can re-relay.
3. **Treat `ExceededMaximumCycles` caused by a peer-supplied cap as `DeclaredWrongCycles`** (i.e., set `is_allowed_relay() = true`) so the relayer can re-request the tx from other peers with the correct cycles.

### Proof of Concept

1. Observe a valid pending transaction `T` with actual script cost of 537 cycles (e.g., standard secp256k1 lock) on the network.
2. Connect to victim node as a peer via `RelayV3`.
3. Announce `T`'s hash via `RelayTransactionHashes`; wait for `GetRelayTransactions`.
4. Send `RelayTransactions` with `T` and `cycles = 1`.
5. Victim node: marks `T`'s hash as known, runs VM with `max_cycles = 1`, gets `ExceededMaximumCycles`, bans the attacker peer, but does **not** clear `T` from the known filter.
6. Honest peer B now tries to relay `T` with correct cycles = 537. The victim's filter check `!tx_filter.contains(&T.hash())` returns `false` → `T` is silently dropped.
7. `T` never enters the victim's tx-pool.

The existing integration test `DeclaredWrongCyclesChunk` confirms the over-declaration path is handled, but the under-declaration path (cycles below actual) produces a different error code that bypasses the re-relay mechanism. [11](#0-10)

### Citations

**File:** sync/src/relayer/transactions_process.rs (L41-42)
```rust
            let mut tx_filter = shared_state.tx_filter();
            tx_filter.remove_expired();
```

**File:** sync/src/relayer/transactions_process.rs (L49-56)
```rust
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
                .collect()
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

**File:** tx-pool/src/process.rs (L513-525)
```rust
                    } else {
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
                        }
                        if reject.is_allowed_relay() {
                            self.send_result_to_relayer(TxVerificationResult::Reject {
                                tx_hash: tx_hash.clone(),
                            });
                        }
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
                    }
```

**File:** tx-pool/src/process.rs (L720-720)
```rust
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

**File:** tx-pool/src/process.rs (L724-732)
```rust
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

**File:** util/types/src/core/tx_pool.rs (L89-113)
```rust
    pub fn is_malformed_tx(&self) -> bool {
        match self {
            Reject::Malformed(_, _) => true,
            Reject::DeclaredWrongCycles(..) => true,
            Reject::Verification(err) => is_malformed_from_verification(err),
            Reject::Resolve(OutPointError::OverMaxDepExpansionLimit) => true,
            _ => false,
        }
    }

    /// Returns true if the reject should be recorded.
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }

    /// Returns true if tx can be resubmitted, allowing relay
    /// * Declared wrong cycles should allow relay with the correct cycles
    /// * Reject but is not malformed and the fee rate reached the threshold,
    ///   it may be due to double spending
    ///   or temporary limitations of the pool resources,
    ///   and expired clearing
    pub fn is_allowed_relay(&self) -> bool {
        matches!(self, Reject::DeclaredWrongCycles(..))
            || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
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
