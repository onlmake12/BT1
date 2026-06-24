Audit Report

## Title
Global `unknown_tx_hashes` Queue Bloat via Insert-Before-Check Ordering Causes Legitimate Peer Banning - (File: `sync/src/types/mod.rs`)

## Summary

`add_ask_for_txs` in `SyncState` unconditionally inserts all attacker-supplied transaction hashes into the global `unknown_tx_hashes` queue before checking whether any size limit has been exceeded. An unprivileged P2P peer can exploit this ordering to leave garbage entries in the queue after being banned, causing any subsequent legitimate peer that sends the maximum allowed batch of hashes to be incorrectly banned with `TooManyUnknownTransactions`. The attacker can repeat this after the 5-minute ban expires, persistently disrupting transaction relay on the targeted node.

## Finding Description

**Root cause — insert-before-check ordering:**

In `sync/src/types/mod.rs`, `add_ask_for_txs` (L1483–1532) first inserts all incoming hashes unconditionally (L1486–1504), then checks the queue length (L1506–1529). The comment at L1506 explicitly acknowledges this: *"Check `unknown_tx_hashes`'s length after inserting the arrival `tx_hashes`"*.

```rust
// L1486-1504: ALL hashes inserted first
for tx_hash in tx_hashes.into_iter().take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER) {
    match unknown_tx_hashes.entry(tx_hash) {
        Vacant(entry) => entry.set_priority(...),  // unconditional insert
        ...
    }
}

// L1506-1529: size check AFTER insertion
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
    || unknown_tx_hashes.len() >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
{
    // count per-peer entries, possibly ban
    if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
        return StatusCode::TooManyUnknownTransactions.into();
    }
    return Status::ignored();
}
```

**Constants (confirmed in `util/constant/src/sync.rs` L68–72):**
- `MAX_RELAY_TXS_NUM_PER_BATCH = 32767`
- `MAX_UNKNOWN_TX_HASHES_SIZE = 50000`
- `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = MAX_RELAY_TXS_NUM_PER_BATCH = 32767`

The per-peer limit equals the per-message limit, so a single message can saturate the per-peer quota in one shot.

**Entry point (confirmed in `sync/src/relayer/transaction_hashes_process.rs` L25–50):**

`TransactionHashesProcess::execute()` calls `state.add_ask_for_txs(self.peer, tx_hashes)` with no pre-insertion guard. The only pre-check is that the message contains `<= MAX_RELAY_TXS_NUM_PER_BATCH` hashes (L29–35), which allows exactly 32,767 hashes through.

**Exploit flow (single-peer scenario — confirmed by existing test):**

1. Attacker connects and sends `RelayTransactionHashes` with 32,767 unique, never-seen hashes. All 32,767 are inserted. Queue = 32,767. Check fires (second condition: `32767 >= 1 * 32767`). Per-peer count = 32,767 ≥ 32,767 → `TooManyUnknownTransactions` → attacker banned (5 minutes per `BAD_MESSAGE_BAN_TIME`). **32,767 garbage entries remain in the queue.**

2. Within the ~10-second cleanup window, a legitimate peer connects and sends 32,767 valid tx hashes. All inserted (queue = 65,534). Check fires (`65534 >= 50000`). Per-peer count for legitimate peer = 32,767 ≥ 32,767 → **legitimate peer banned**.

**Multi-peer scenario (N > 1 peers connected):**

The attacker sends two messages of 32,767 hashes each. After the first, the second condition (`32767 >= N * 32767`) does not fire for N > 1. After the second, the first condition (`65534 >= 50000`) fires, attacker is banned with 65,534 garbage entries remaining. A legitimate peer sending 32,767 hashes then reaches queue size 98,301, triggering the check and being banned.

**No cleanup on ban:** There is no code path that removes a peer's entries from `unknown_tx_hashes` upon banning. Cleanup only occurs in `pop_ask_for_txs` (L1453–1481) when `next_request_peer()` returns `None`, which happens only at the next timer tick (~10 seconds, `ASK_FOR_TXS_TOKEN`). The garbage window is confirmed by the structure of `pop_ask_for_txs`.

**`tx_filter` does not prevent this:** It only tracks hashes of seen/verified transactions. The attacker uses fresh, never-seen hashes each reconnection.

## Impact Explanation

Any legitimate peer sending the maximum protocol-allowed batch of 32,767 transaction hashes while the queue is bloated is incorrectly banned with `TooManyUnknownTransactions`. This is a false-positive ban that prevents the legitimate peer from relaying transactions to the victim node. The attacker can repeat this every 5 minutes (after ban expiry) with minimal resources, persistently disrupting transaction propagation on the targeted node. If applied to many nodes simultaneously, this constitutes **CKB network congestion with few costs** — matching the High impact class (10001–15000 points): *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation

- Requires only a standard P2P connection — no keys, no hashpower, no privilege.
- Only 1–2 `RelayTransactionHashes` messages are needed per attack cycle.
- The per-peer rate limiter (30 req/s) is irrelevant; only 1–2 messages are required.
- The attacker reconnects after the 5-minute `BAD_MESSAGE_BAN_TIME` and repeats indefinitely.
- The attack is stateless from the attacker's perspective: fresh unique hashes are trivially generated.
- The existing integration test (`test/src/specs/relay/too_many_unknown_transactions.rs`) confirms the banning behavior is real and reproducible.

## Recommendation

Move the size-limit check **before** insertion:

1. Before the insertion loop, count how many of the incoming hashes are already present in `unknown_tx_hashes` for this peer.
2. If the peer's current contribution already meets or exceeds `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, return `TooManyUnknownTransactions` immediately without inserting anything.
3. If the global queue is already at or above `MAX_UNKNOWN_TX_HASHES_SIZE`, return `Status::ignored()` without inserting anything.
4. Additionally, consider cleaning up a banned peer's entries from `unknown_tx_hashes` at disconnect/ban time to eliminate the garbage window entirely.

## Proof of Concept

The existing test at `test/src/specs/relay/too_many_unknown_transactions.rs` already demonstrates step 1 (attacker banned after sending 32,767 hashes). To demonstrate the full exploit:

1. Run a CKB node locally.
2. Connect as peer A (attacker). Send `RelayTransactionHashes` with 32,767 unique non-existent tx hashes. Observe: peer A is banned, queue contains 32,767 garbage entries.
3. Immediately (within ~10 seconds) connect as peer B (legitimate). Send `RelayTransactionHashes` with 32,767 valid tx hashes.
4. Observe: peer B is banned with `TooManyUnknownTransactions` despite sending a protocol-compliant message.
5. Repeat from step 2 after 5 minutes to confirm indefinite repeatability.

The constants and code path are fully confirmed in the repository. The only external dependency is the ability to open a standard P2P connection to the node.