All cited code references check out exactly against the actual source. Let me verify the key behavioral claims:

All code references are verified against the actual source. Every cited line, behavior, and comparison is accurate. Here is the validation result:

---

Audit Report

## Title
Missing Duplicate-Hash Deduplication in `GetTransactionsProofProcess::execute` Enables DB Read Amplification — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary
`GetTransactionsProofProcess::execute` does not deduplicate incoming `tx_hashes` before performing per-hash DB lookups, unlike `GetBlocksProofProcess::execute` which explicitly rejects duplicates with a `MalformedProtocolMessage` ban. An unprivileged remote peer can send a single `GetTransactionsProof` message with 1000 copies of the same valid on-chain transaction hash, causing 2000 redundant DB reads and CBMT proof construction over 1000 duplicate indices, with no ban or rate-limit consequence.

## Finding Description
`GetBlocksProofProcess::execute` collects all incoming block hashes and immediately returns `StatusCode::MalformedProtocolMessage` (a 4xx code triggering a 5-minute peer ban) if any duplicate is detected:

```rust
// get_blocks_proof.rs lines 62-70
let mut uniq = HashSet::new();
if !block_hashes
    .iter()
    .chain([last_block_hash].iter())
    .all(|hash| uniq.insert(hash))
{
    return StatusCode::MalformedProtocolMessage
        .with_context("duplicate block hash exists");
}
```

`GetTransactionsProofProcess::execute` has no equivalent guard. After the size check (`> 1000` → reject), it directly partitions the raw iterator:

```rust
// get_transactions_proof.rs lines 54-75
let (found, missing): (Vec<_>, Vec<_>) = self
    .message
    .tx_hashes()
    .to_entity()
    .into_iter()
    .partition(|tx_hash| {
        snapshot
            .get_transaction_info(tx_hash)   // DB read #1 per hash
            ...
    });

for tx_hash in found {
    let (tx, tx_info) = snapshot
        .get_transaction_with_info(&tx_hash)  // DB read #2 per hash
        .expect("tx exists");
    txs_in_blocks
        .entry(tx_info.block_hash)
        .or_insert_with(Vec::new)
        .push((tx, tx_info.index));           // duplicate indices accumulate
}
```

With 1000 copies of the same valid tx hash:
- The `partition` closure calls `get_transaction_info` 1000 times (DB read #1 × 1000).
- The `for tx_hash in found` loop calls `get_transaction_with_info` 1000 times (DB read #2 × 1000).
- All 1000 entries share the same `block_hash` key, so the Vec for that block accumulates 1000 `(tx, same_index)` entries.
- `CBMT::build_merkle_proof` is then called with 1000 duplicate indices.

The handler concludes by calling `reply_proof::<packed::SendTransactionsProofV1>`, which returns `Status::ok()` (code 200). In `lib.rs` lines 81–86, `status.should_ban()` is called; since `should_ban` only returns `Some` for 4xx codes (`status.rs` lines 95–101), the peer is never banned and the connection remains open for indefinite repetition.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. The attack requires only a single P2P connection and one valid on-chain transaction hash — no PoW, no stake, no privilege. Each max-size message amplifies DB reads by 1000× relative to a legitimate single-hash request. An attacker can loop this continuously with no penalty, saturating the RocksDB read path of the light-client protocol server and degrading its ability to serve legitimate light clients. Multiple concurrent connections multiply the effect linearly.

## Likelihood Explanation
The light-client protocol endpoint is reachable by any unprivileged peer. The message is structurally valid (passes the `> 1000` size check with exactly 1000 hashes), requires no special knowledge beyond one valid on-chain tx hash, and the peer is never banned. The attack is trivially repeatable in a tight loop from a single connection.

## Recommendation
Add a deduplication check in `GetTransactionsProofProcess::execute` immediately after the size check, mirroring the pattern already present in `GetBlocksProofProcess::execute`:

```rust
let tx_hashes: Vec<_> = self.message.tx_hashes().to_entity().into_iter().collect();

let mut uniq = HashSet::new();
if !tx_hashes.iter().all(|h| uniq.insert(h)) {
    return StatusCode::MalformedProtocolMessage
        .with_context("duplicate tx hash exists");
}
```

This returns a 4xx status, which `should_ban` converts into a 5-minute peer ban, consistent with the blocks-proof handler's behavior.

## Proof of Concept
1. Connect to a CKB node's light-client P2P endpoint.
2. Identify any valid on-chain transaction hash `H`.
3. Build a `GetTransactionsProof` message with `tx_hashes = [H] * 1000` and a valid `last_hash`.
4. Send the message repeatedly in a loop.
5. Observe: the server performs 2000 DB reads per message, returns `SendTransactionsProofV1` (not a ban), and the peer connection remains open.
6. Compare DB read counters with a baseline request of `tx_hashes = [H]` (2 DB reads): the amplification factor is 1000×.

A unit test can be added alongside the existing tests in `util/light-client-protocol-server/src/tests/components/get_transactions_proof.rs`, constructing a `GetTransactionsProof` message with 1000 copies of the same hash and asserting that the peer is banned (i.e., `nc.not_banned(peer_index)` returns `false`).