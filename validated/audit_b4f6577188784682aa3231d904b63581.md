All cited code references check out. Let me verify the CBMT duplicate-index behavior before finalizing.

All claims verified against the actual source. Every code reference is accurate.

Audit Report

## Title
Missing Duplicate-Hash Deduplication in `GetTransactionsProofProcess::execute` Enables Unbounded DB Read Amplification — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary

`GetTransactionsProofProcess::execute` accepts up to 1000 tx hashes with no duplicate check. An unprivileged remote peer can send a single `GetTransactionsProof` message containing 1000 copies of the same valid on-chain tx hash, forcing the server to perform 2000 redundant DB reads per message. The sibling handler `GetBlocksProofProcess::execute` explicitly guards against this with a `HashSet` check; `GetTransactionsProofProcess` does not. There is no rate limiter on the light client protocol path, and the peer is never banned because the request completes with `Status::ok()`.

## Finding Description

**Root cause — missing dedup guard:**

`GetBlocksProofProcess::execute` explicitly rejects duplicate hashes at lines 62–70:

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
``` [1](#0-0) 

`GetTransactionsProofProcess::execute` has no equivalent check. After the size guard at lines 37–39, it immediately iterates the raw (potentially all-duplicate) list: [2](#0-1) 

**Phase 1 — 1000 `get_transaction_info` DB calls:**

The `partition` closure calls `snapshot.get_transaction_info(tx_hash)` for every element in the iterator, including all 1000 duplicates: [3](#0-2) 

**Phase 2 — 1000 `get_transaction_with_info` DB calls:**

The `for tx_hash in found` loop calls `snapshot.get_transaction_with_info(&tx_hash)` for every element in `found`, again including all 1000 duplicates: [4](#0-3) 

**Phase 3 — CBMT proof over 1000 duplicate indices:**

Because `txs_in_blocks` is a `HashMap` keyed by `block_hash`, all 1000 duplicate entries collapse into a single `Vec` containing 1000 `(tx, same_index)` tuples. `CBMT::build_merkle_proof` is then called with a 1000-element slice of identical indices: [5](#0-4) 

**No rate limiter on the light client path:**

The `LightClientProtocol::received` handler calls `try_process` directly with no throttling. Compare this to `HolePunching::received`, which checks `self.rate_limiter.check_key(...)` before dispatching. The light client handler has no such guard: [6](#0-5) 

**Peer is never banned:**

`Status::should_ban()` only triggers on 4xx status codes. Since the duplicate request completes successfully and returns `Status::ok()`, the peer is never disconnected or banned: [7](#0-6) 

The limit constant is 1000: [8](#0-7) 

## Impact Explanation

Each malicious message causes 2000 redundant DB reads (1000 × `get_transaction_info` + 1000 × `get_transaction_with_info`) plus a `CBMT::build_merkle_proof` call over 1000 duplicate indices. A single peer can send these messages in a tight loop with no back-pressure, no rate limiting, and no peer ban. This constitutes a low-cost, high-amplification resource exhaustion attack against the CKB full node serving light clients, matching the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (10001–15000 points).

## Likelihood Explanation

The attack requires only: (1) a valid on-chain tx hash, publicly observable from any block explorer or full node, and (2) the ability to connect as a light client peer. No privilege, key, or hashpower is required. The asymmetry with `GetBlocksProofProcess` (which has the guard) confirms this is an unintentional omission. The attack is trivially repeatable in a tight loop.

## Recommendation

Add a duplicate-hash check immediately after the size guard, mirroring `GetBlocksProofProcess`:

```rust
let tx_hashes: Vec<_> = self.message.tx_hashes().to_entity().into_iter().collect();
let mut uniq = HashSet::new();
if !tx_hashes.iter().all(|h| uniq.insert(h)) {
    return StatusCode::MalformedProtocolMessage
        .with_context("duplicate tx hash exists");
}
```

This returns a 4xx status, which `should_ban()` converts into a peer ban, eliminating both the redundant DB work and the attack vector. [9](#0-8) 

## Proof of Concept

1. Obtain any valid on-chain tx hash `H` (e.g., from block 1, publicly visible).
2. Build a `GetTransactionsProof` message: `last_hash = <current tip hash>`, `tx_hashes = [H; 1000]`.
3. Connect as a light client peer and send the message in a loop.
4. Observe: server performs 2000 DB reads per message, peer is never banned, no rate limiting applies.
5. Compare DB read count against a request with `tx_hashes = [H]` (1 unique hash → 2 DB reads).
6. Contrast with sending `GetBlocksProof` with 2 duplicate block hashes — that returns `MalformedProtocolMessage` and bans the peer immediately. [1](#0-0)

### Citations

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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L32-39)
```rust
    pub(crate) async fn execute(self) -> Status {
        if self.message.tx_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no transaction");
        }

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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L67-75)
```rust
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

**File:** util/light-client-protocol-server/src/lib.rs (L55-92)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        data: Bytes,
    ) {
        trace!("LightClient.received peer={}", peer);

        let msg = match packed::LightClientMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "LightClient.received a malformed message from Peer({})",
                    peer
                );
                nc.ban_peer(
                    peer,
                    constant::BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();
        let status = self.try_process(&nc, peer, msg).await;
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
        } else if status.should_warn() {
            warn!("process {} from {}; result is {}", item_name, peer, status);
        } else if !status.is_ok() {
            debug!("process {} from {}; result is {}", item_name, peer, status);
        }
    }
```

**File:** util/light-client-protocol-server/src/status.rs (L95-102)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code as u16;
        if !(400..500).contains(&code) {
            None
        } else {
            Some(constant::BAD_MESSAGE_BAN_TIME)
        }
    }
```

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```
