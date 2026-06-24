All seven code citations verify exactly against the source. Let me confirm the `reply_proof` return path:

`reply_proof` in `lib.rs` always returns `Status::ok()` at line 236 unless an internal error (5xx) occurs. [1](#0-0) 

5xx codes do not trigger `should_ban()` (only 4xx do), confirming the peer is never banned. [2](#0-1) 

---

All claims hold. The full exploit chain is confirmed:

- `get_blocks_proof.rs` has the HashSet dedup guard at L62–70. [3](#0-2) 
- `get_transactions_proof.rs` has no equivalent guard — only the `> 1000` size check. [4](#0-3) 
- 1000 duplicate hashes each trigger `get_transaction_info` (DB read #1) in the partition. [5](#0-4) 
- Each then triggers `get_transaction_with_info` (DB read #2) and pushes into `txs_in_blocks`. [6](#0-5) 
- `CBMT::build_merkle_proof` is called with 1000 duplicate indices. [7](#0-6) 
- The handler returns `Status::ok()`, the ban path in `received` never fires. [8](#0-7) 

---

Audit Report

## Title
Missing Duplicate-Hash Deduplication in `GetTransactionsProofProcess::execute` Enables DB Read Amplification — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary
`GetTransactionsProofProcess::execute` does not deduplicate incoming `tx_hashes` before performing per-hash DB lookups, unlike `GetBlocksProofProcess::execute` which explicitly bans peers on duplicate hashes via `MalformedProtocolMessage`. An unprivileged remote peer can send a single `GetTransactionsProof` message with 1000 copies of the same valid on-chain transaction hash, triggering 2000 DB reads and unnecessary CBMT proof work, with no ban or rate-limit consequence, indefinitely repeatable.

## Finding Description
`GetBlocksProofProcess::execute` inserts all incoming hashes into a `HashSet` and returns `StatusCode::MalformedProtocolMessage` (4xx → ban) on any duplicate: [3](#0-2) 

`GetTransactionsProofProcess::execute` has no equivalent guard. After the `> 1000` size check, it directly partitions the raw iterator, calling `get_transaction_info` once per hash (DB read #1): [5](#0-4) 

Then for every hash in `found`, it calls `get_transaction_with_info` (DB read #2) and pushes into `txs_in_blocks`, accumulating 1000 duplicate `(tx, same_index)` entries for the same block: [6](#0-5) 

`CBMT::build_merkle_proof` is then called with 1000 duplicate indices: [7](#0-6) 

The handler returns `Status::ok()` (200). `should_ban` only triggers on 4xx codes: [2](#0-1) 

So the peer is never penalized and the attack is indefinitely repeatable. The ban path in `received` only fires when `status.should_ban()` returns `Some`: [8](#0-7) 

## Impact Explanation
Each max-size duplicate-hash message causes 2000 RocksDB reads (1000 `get_transaction_info` + 1000 `get_transaction_with_info`) against a legitimate baseline of 2 reads — a 1000x amplification. The light-client server shares the RocksDB instance with the main node. A single persistent P2P connection sending messages in a tight loop can continuously saturate the node's read I/O. Multiple concurrent connections multiply the effect linearly. This matches the allowed impact: **High — vulnerabilities or bad designs which could cause CKB network congestion with few costs**, given the trivial attacker cost (one valid tx hash, one connection) versus the amplified server-side work.

## Likelihood Explanation
The light-client protocol is reachable by any unprivileged peer. The message is structurally valid (passes the `> 1000` size check), requires no PoW or stake, and the peer is never banned. The attack is trivially repeatable in a tight loop with a single connection. [9](#0-8) 

## Recommendation
Add a deduplication check in `GetTransactionsProofProcess::execute` immediately after the size check, mirroring the pattern in `GetBlocksProofProcess::execute`:

```rust
let mut uniq = HashSet::new();
if !tx_hashes.iter().all(|h| uniq.insert(h)) {
    return StatusCode::MalformedProtocolMessage
        .with_context("duplicate tx hash exists");
}
```

This returns a 4xx status, which `should_ban` converts into a 5-minute peer ban consistent with the blocks-proof handler's behavior. [3](#0-2) 

## Proof of Concept
1. Connect to a CKB node's light-client P2P endpoint.
2. Identify any valid on-chain transaction hash `H`.
3. Build a `GetTransactionsProof` message with `tx_hashes = [H] * 1000` and a valid `last_hash`.
4. Send the message repeatedly in a loop.
5. Observe: the server performs 2000 DB reads per message, returns `SendTransactionsProofV1` (200 OK), and the peer connection remains open indefinitely.
6. Compare DB read counters with a baseline request of `tx_hashes = [H]` (2 DB reads): the amplification factor is 1000x. [4](#0-3)

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L81-86)
```rust
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
```

**File:** util/light-client-protocol-server/src/lib.rs (L235-237)
```rust
        nc.reply(peer, &message).await;
        Status::ok()
    }
```

**File:** util/light-client-protocol-server/src/status.rs (L95-101)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code as u16;
        if !(400..500).contains(&code) {
            None
        } else {
            Some(constant::BAD_MESSAGE_BAN_TIME)
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L62-70)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L54-64)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = self
            .message
            .tx_hashes()
            .to_entity()
            .into_iter()
            .partition(|tx_hash| {
                snapshot
                    .get_transaction_info(tx_hash)
                    .map(|tx_info| snapshot.is_main_chain(&tx_info.block_hash))
                    .unwrap_or_default()
            });
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L66-75)
```rust
        let mut txs_in_blocks = HashMap::new();
        for tx_hash in found {
            let (tx, tx_info) = snapshot
                .get_transaction_with_info(&tx_hash)
                .expect("tx exists");
            txs_in_blocks
                .entry(tx_info.block_hash)
                .or_insert_with(Vec::new)
                .push((tx, tx_info.index));
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L86-97)
```rust
            let merkle_proof = CBMT::build_merkle_proof(
                &block
                    .transactions()
                    .iter()
                    .map(|tx| tx.hash())
                    .collect::<Vec<_>>(),
                &txs_and_tx_indices
                    .iter()
                    .map(|(_, index)| *index as u32)
                    .collect::<Vec<_>>(),
            )
            .expect("build proof with verified inputs should be OK");
```

**File:** util/light-client-protocol-server/src/constant.rs (L5-7)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```
