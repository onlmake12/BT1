Audit Report

## Title
`DeclaredWrongCycles` Simultaneously Classified as Malformed and Recoverable Causes Unjustified 3-Day Peer Ban — (`util/types/src/core/tx_pool.rs`, `tx-pool/src/process.rs`)

## Summary

`Reject::DeclaredWrongCycles` is classified as a malformed transaction in `is_malformed_tx()`, triggering a 3-day peer ban via `ban_malformed`, while simultaneously being classified as a recoverable, re-relayable error in `is_allowed_relay()`. Because `after_process` evaluates both flags independently, a peer that sends a valid transaction with an incorrect declared cycle count is banned for 3 days even though the protocol explicitly acknowledges the error is recoverable. During any CKB software upgrade that adjusts VM cycle accounting, honest peers running a slightly older version will be mass-banned, degrading network connectivity and transaction propagation.

## Finding Description

In `util/types/src/core/tx_pool.rs` at line 92, `is_malformed_tx()` returns `true` for `DeclaredWrongCycles`:

```rust
Reject::DeclaredWrongCycles(..) => true,
```

Yet `is_allowed_relay()` at lines 110–112 explicitly carves out `DeclaredWrongCycles` as recoverable, with the comment "Declared wrong cycles should allow relay with the correct cycles":

```rust
pub fn is_allowed_relay(&self) -> bool {
    matches!(self, Reject::DeclaredWrongCycles(..))
        || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
}
```

In `tx-pool/src/process.rs` at lines 514–521, `after_process` evaluates both flags independently with no mutual exclusion:

```rust
if reject.is_malformed_tx() {
    self.ban_malformed(peer, format!("reject {reject}")).await;
}
if reject.is_allowed_relay() {
    self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash: tx_hash.clone() });
}
```

For `DeclaredWrongCycles`, both branches fire: the peer is banned AND the relayer is notified the tx can be re-relayed. The reject is generated at lines 736–748 of `process.rs` whenever `declared != verified.cycles`. The ban duration is `DEFAULT_BAN_TIME = Duration::from_secs(3600 * 24 * 3)` (3 days) defined in `sync/src/relayer/transactions_process.rs` line 13.

The integration test `DeclaredWrongCyclesAndRelayAgain` at `test/src/specs/tx_pool/declared_wrong_cycles.rs` line 91 explicitly confirms the ban is triggered:

```rust
let ret = wait_until(10, || node0.rpc_client().get_peers().is_empty());
assert!(ret, "The address of net should be removed from node0's peers");
```

## Impact Explanation

This is a **High** severity finding matching "Vulnerabilities or bad designs which could cause CKB network congestion with few costs." During any CKB software upgrade that modifies VM cycle accounting (e.g., a hardfork or bug fix), nodes running the old version will declare cycle counts that differ from nodes running the new version. Every such peer is banned for 3 days on contact. With enough peers on the old version, this causes mass disconnection across the network, degrading transaction propagation and peer connectivity at network-upgrade boundaries — a predictable, repeatable, low-cost disruption requiring no special attacker capability.

## Likelihood Explanation

Triggerable by any unprivileged P2P peer via a `RelayTransactions` message. The condition `declared != verified.cycles` arises naturally during any CKB upgrade touching cycle costs, or when a peer's local VM state diverges. No attacker capability is required; honest peers on a slightly older software version trigger this automatically. The integration test confirms the ban is reliably and deterministically triggered.

## Recommendation

Remove `Reject::DeclaredWrongCycles(..) => true` from `is_malformed_tx()` in `util/types/src/core/tx_pool.rs`. The protocol already correctly identifies this as a recoverable error via `is_allowed_relay()`. If any penalty is warranted, apply a short-duration soft penalty rather than the 3-day hard ban reserved for structurally malformed transactions. The two classification functions are logically contradictory for this variant and the malformed classification must be corrected to match the relay policy.

## Proof of Concept

1. Connect a custom peer to a CKB node via `RelayV3`.
2. Announce a valid transaction hash via `RelayTransactionHashes`.
3. When the node responds with `GetRelayTransactions`, send the transaction with `declared_cycles = actual_cycles + 1`.
4. Observe: the node rejects with `DeclaredWrongCycles`, calls `ban_malformed`, and the peer is disconnected and banned for 3 days.

This exact flow is exercised by the existing integration test `DeclaredWrongCyclesAndRelayAgain` in `test/src/specs/tx_pool/declared_wrong_cycles.rs` (line 90: `relay_tx(&net, node0, tx.clone(), ALWAYS_SUCCESS_SCRIPT_CYCLE + 1)`), which asserts at line 91–94 that the peer is removed from node0's peers list — confirming the ban fires reliably.