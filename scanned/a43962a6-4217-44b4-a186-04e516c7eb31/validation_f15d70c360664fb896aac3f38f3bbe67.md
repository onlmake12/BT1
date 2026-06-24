Audit Report

## Title
`DeclaredWrongCycles` Simultaneously Classified as Malformed and Recoverable Causes Disproportionate 3-Day Peer Ban — (`util/types/src/core/tx_pool.rs`, `tx-pool/src/process.rs`)

## Summary

`Reject::DeclaredWrongCycles` is classified as a malformed transaction in `is_malformed_tx()` while simultaneously being explicitly carved out as a recoverable, relay-allowed error in `is_allowed_relay()`. In `after_process`, both branches fire independently, causing the sending peer to be banned for 3 days even though the protocol's own relay logic acknowledges the transaction is valid and can be re-relayed with correct cycles. The integration test `DeclaredWrongCyclesAndRelayAgain` confirms the ban is reliably triggered.

## Finding Description

In `util/types/src/core/tx_pool.rs`, `is_malformed_tx()` returns `true` for `DeclaredWrongCycles`: [1](#0-0) 

The same file's `is_allowed_relay()` explicitly carves out `DeclaredWrongCycles` as recoverable, with the comment "Declared wrong cycles should allow relay with the correct cycles": [2](#0-1) 

In `tx-pool/src/process.rs`, `after_process` evaluates both flags independently with no mutual exclusion — for a `DeclaredWrongCycles` reject, both branches fire: [3](#0-2) 

The reject is generated when the peer-declared cycle count does not match the locally verified count: [4](#0-3) 

The ban duration is 3 days: [5](#0-4) 

The integration test `DeclaredWrongCyclesAndRelayAgain` explicitly asserts that the peer is removed from node0's peer list after sending wrong cycles, confirming the ban is intentionally triggered: [6](#0-5) 

## Impact Explanation

This is a **High** severity bad design that can cause CKB network connectivity degradation with minimal cost. During any CKB software upgrade that adjusts VM cycle accounting (e.g., a hardfork or bug fix), nodes running the old version will declare cycles that differ from what nodes running the new version compute. Every such relay attempt results in a 3-day ban. If a significant portion of the network is mid-upgrade, nodes on different versions will systematically ban each other, fragmenting the peer graph and impairing transaction propagation across the network. The cost to trigger this is zero — it occurs naturally during upgrades without any attacker involvement, and any unprivileged P2P peer can trigger it deliberately by sending `declared_cycles ≠ actual_cycles` by even 1.

## Likelihood Explanation

Reachable by any unprivileged peer via the `RelayV3` protocol with a single `RelayTransactions` message. The trigger condition arises naturally during software upgrades that touch cycle costs, and is trivially reproducible deliberately. The existing integration test confirms reliable triggering. No special privileges, leaked keys, or victim mistakes are required.

## Recommendation

Remove `Reject::DeclaredWrongCycles(..) => true` from `is_malformed_tx()` in `util/types/src/core/tx_pool.rs`. The protocol already correctly identifies this as a recoverable error via `is_allowed_relay()`. If a penalty is warranted, apply a short-duration warning or score penalty rather than the full 3-day malformed-transaction ban. The two classification functions are logically contradictory for this variant and the malformed classification should be corrected to match the relay policy.

## Proof of Concept

1. Connect a custom peer to a CKB node via `RelayV3`.
2. Announce a valid transaction hash via `RelayTransactionHashes`.
3. When the node responds with `GetRelayTransactions`, send the transaction with `declared_cycles = actual_cycles + 1`.
4. Observe: the node bans the peer for 3 days.

This is directly exercised by the existing integration test `DeclaredWrongCyclesAndRelayAgain` at `test/src/specs/tx_pool/declared_wrong_cycles.rs` line 90–94, which asserts `node0.rpc_client().get_peers().is_empty()` — confirming the peer ban is reliably triggered. [7](#0-6)

### Citations

**File:** util/types/src/core/tx_pool.rs (L89-97)
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
```

**File:** util/types/src/core/tx_pool.rs (L110-113)
```rust
    pub fn is_allowed_relay(&self) -> bool {
        matches!(self, Reject::DeclaredWrongCycles(..))
            || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
    }
```

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

**File:** tx-pool/src/process.rs (L736-748)
```rust
        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
```

**File:** sync/src/relayer/transactions_process.rs (L13-13)
```rust
const DEFAULT_BAN_TIME: Duration = Duration::from_secs(3600 * 24 * 3);
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
