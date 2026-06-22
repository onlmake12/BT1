### Title
`DeclaredWrongCycles` Misclassified as Malformed Transaction Causes Unfair Peer Ban — (`tx-pool/src/process.rs`, `util/types/src/core/tx_pool.rs`)

---

### Summary

The `Reject::DeclaredWrongCycles` error is simultaneously classified as both a "malformed transaction" (triggering a 3-day peer ban) and an "allowed relay" (explicitly acknowledging the transaction is valid and can be re-relayed with correct cycles). This internal contradiction means a peer can be permanently banned for a recoverable error that may be outside its control, directly analogous to the external report's pattern of misattributing fault to a party for errors they did not cause.

---

### Finding Description

In `util/types/src/core/tx_pool.rs`, the `Reject` enum's `is_malformed_tx()` method classifies `DeclaredWrongCycles` as a malformed transaction:

```rust
pub fn is_malformed_tx(&self) -> bool {
    match self {
        Reject::Malformed(_, _) => true,
        Reject::DeclaredWrongCycles(..) => true,   // ← classified as malformed
        ...
    }
}
``` [1](#0-0) 

Yet the same file's `is_allowed_relay()` explicitly carves out `DeclaredWrongCycles` as a *recoverable* error, with the comment "Declared wrong cycles should allow relay with the correct cycles":

```rust
pub fn is_allowed_relay(&self) -> bool {
    matches!(self, Reject::DeclaredWrongCycles(..))   // ← explicitly recoverable
        || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
}
``` [2](#0-1) 

In `tx-pool/src/process.rs`, the `after_process` function evaluates both flags independently. For a remote peer that sends a transaction with wrong declared cycles, both branches fire:

```rust
if reject.is_malformed_tx() {
    self.ban_malformed(peer, format!("reject {reject}")).await;  // peer banned
}
if reject.is_allowed_relay() {
    self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash: tx_hash.clone() });
}
``` [3](#0-2) 

The `DeclaredWrongCycles` reject itself is generated in `_process_tx` when the peer-declared cycle count does not match the locally verified count:

```rust
if let Some(declared) = declared_cycles
    && declared != verified.cycles
{
    return Some((Err(Reject::DeclaredWrongCycles(declared, verified.cycles)), snapshot));
}
``` [4](#0-3) 

The ban duration applied by `ban_malformed` originates from the relay layer's `DEFAULT_BAN_TIME`:

```rust
const DEFAULT_BAN_TIME: Duration = Duration::from_secs(3600 * 24 * 3);
``` [5](#0-4) 

---

### Impact Explanation

A peer is banned for **3 days** whenever it relays a transaction whose declared cycle count does not exactly match the receiving node's locally computed cycle count. The transaction itself may be entirely valid — the protocol acknowledges this by setting `is_allowed_relay() = true`. The ban is therefore disproportionate and misattributes fault.

Concrete scenarios where the mismatch is outside the relaying peer's control:

1. **VM version skew during upgrades**: If two nodes run different minor versions of `ckb-vm` where a cycle-cost constant was adjusted (e.g., a bug fix or hardfork preparation), the same script produces different cycle counts. The relaying peer computes and declares cycles correctly per its own version, but is banned by the receiving node running a newer version.

2. **Honest intermediary relay**: A peer that received a transaction from a third party, verified it locally (getting cycles `C`), and relays it with declared cycles `C` will be banned if the receiving node's VM produces a different count `C'`. The relaying peer had no way to know `C ≠ C'` ahead of time.

The result is that honest peers operating on a slightly different software version are silently ejected from the network for 3 days, degrading network connectivity and transaction propagation without any protocol-level recourse.

---

### Likelihood Explanation

This is reachable by any unprivileged P2P peer submitting a `RelayTransactions` message. The trigger condition — declared cycles ≠ actual cycles — occurs naturally during any CKB software upgrade that touches VM cycle accounting, or when a peer's local VM state diverges. The integration test `DeclaredWrongCyclesAndRelayAgain` in `test/src/specs/tx_pool/declared_wrong_cycles.rs` demonstrates the ban is reliably triggered and confirmed. [6](#0-5) 

---

### Recommendation

Remove `Reject::DeclaredWrongCycles(..) => true` from `is_malformed_tx()` in `util/types/src/core/tx_pool.rs`. The protocol already correctly identifies this as a recoverable error via `is_allowed_relay()`. The peer should receive a warning or a short-duration penalty (if any), not a 3-day ban equivalent to sending a structurally malformed transaction. The two classification functions are logically contradictory for this variant and the malformed classification should be corrected to match the relay policy. [7](#0-6) 

---

### Proof of Concept

1. Connect a custom peer to a CKB node via the `RelayV3` protocol.
2. Announce a valid transaction hash via `RelayTransactionHashes`.
3. When the node responds with `GetRelayTransactions`, send the transaction with `declared_cycles = actual_cycles + 1`.
4. Observe: the node rejects the transaction with `DeclaredWrongCycles` and bans the peer for 3 days, even though the transaction is valid and `is_allowed_relay()` returns `true` for this reject variant.

This exact flow is exercised by the existing integration test `DeclaredWrongCycles` in `test/src/specs/tx_pool/declared_wrong_cycles.rs` (line 26: `relay_tx(&net, node0, tx, ALWAYS_SUCCESS_SCRIPT_CYCLE + 1)`), which asserts the transaction is rejected — but does not assert or check whether the peer ban is appropriate given the transaction's validity. [8](#0-7)

### Citations

**File:** util/types/src/core/tx_pool.rs (L87-113)
```rust
impl Reject {
    /// Returns true if the reject reason is malformed tx.
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

**File:** test/src/specs/tx_pool/declared_wrong_cycles.rs (L10-33)
```rust
impl Spec for DeclaredWrongCycles {
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
