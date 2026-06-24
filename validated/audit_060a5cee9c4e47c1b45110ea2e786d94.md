Audit Report

## Title
Unbounded Per-Message I/O Work with No Per-Peer Rate Limit in `GetBlocksProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary
The light-client protocol server accepts `GetBlocksProof` messages from any peer and performs up to 4 × 1000 synchronous RocksDB reads plus one `mmr.gen_proof(1000 positions)` call per message. Because structurally valid messages always return `StatusCode::OK` (200), the ban logic is never triggered, and no per-peer rate limit or message-frequency guard exists anywhere in the server. A single unprivileged peer can sustain a continuous flood of such messages at negligible cost, saturating the node's I/O and CPU.

## Finding Description
`GET_BLOCKS_PROOF_LIMIT = 1000` in `util/light-client-protocol-server/src/constant.rs` (L5) caps work per message but places no bound on message frequency. [1](#0-0) 

Inside `GetBlocksProofProcess::execute()`, the handler partitions hashes via `snapshot.is_main_chain` (L74), then for each found hash calls `snapshot.get_block_header` (L83), `snapshot.get_block_uncles` (L89), and `snapshot.get_block_extension` (L91) — three to four synchronous DB reads per hash, up to 1000 hashes. [2](#0-1) 

After the loop, `reply_proof` in `lib.rs` (L210) calls `mmr.gen_proof(items_positions)` with up to 1000 positions. [3](#0-2) 

`Status::should_ban()` in `status.rs` (L95–102) only returns `Some(ban_time)` for HTTP-class 4xx codes. A well-formed request with valid hashes returns `StatusCode::OK` (200), so the dispatch loop in `lib.rs` (L81–86) never calls `nc.ban_peer`. [4](#0-3) [5](#0-4) 

The `LightClientProtocol` struct holds only a `Shared`; there is no per-peer counter, token bucket, timestamp guard, or inflight-request cap. [6](#0-5) 

A grep for `rate_limit`, `throttle`, `quota`, `message_count`, `flood`, or `per_peer` across the entire `util/light-client-protocol-server/` tree returns zero matches, confirming the complete absence of any throttle mechanism.

The same structural pattern and the same `GET_*_LIMIT = 1000` constant apply to `GetTransactionsProofProcess` and `GetLastStateProofProcess`. [7](#0-6) 

## Impact Explanation
A single attacker peer can sustain a continuous stream of valid `GetBlocksProof` messages, each forcing ~4000 synchronous RocksDB reads and a 1000-position MMR proof generation on the server. This saturates the node's RocksDB I/O bandwidth and CPU, starving both the chain-sync pipeline and legitimate light-client peers. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** The attacker's cost is sending ~32 KB messages; the server's cost per message is orders of magnitude higher.

## Likelihood Explanation
The attack requires only a valid P2P connection to a node with the light-client protocol enabled. No proof-of-work, no key, no privileged role is needed. The 1000 valid main-chain block hashes required per message are trivially obtained by syncing a few headers first. The existing unit test at `util/light-client-protocol-server/src/tests/components/get_blocks_proof.rs` L78 explicitly asserts `nc.not_banned(peer_index)` for a valid request, confirming the absence of any ban or throttle on the happy path. [8](#0-7) 

The attack is fully repeatable and requires no victim mistake.

## Recommendation
1. **Per-peer message-rate limit**: Track the timestamp of the last `GetBlocksProof` message per peer and enforce a minimum inter-message interval (e.g., 1 request/second). Return a 4xx status (triggering a ban) or silently drop messages that exceed the limit.
2. **Inflight request cap**: Allow at most one outstanding `GetBlocksProof` per peer; drop or ban if a second arrives before the first response is sent.
3. **Reduce the per-message ceiling**: Lower `GET_BLOCKS_PROOF_LIMIT` from 1000 to a smaller value (e.g., 100–200) to reduce per-message cost while still serving legitimate clients.
4. **Apply the same fix to `GetTransactionsProof` and `GetLastStateProof`**, which share the identical structural pattern and the same `GET_*_LIMIT = 1000` constant.

## Proof of Concept
```
1. Connect to a CKB node with the light-client protocol enabled.
2. Sync enough headers to collect 1000 valid main-chain block hashes H[0..999].
3. In a tight loop:
     msg = GetBlocksProof { last_hash: tip_hash, block_hashes: H[0..999] }
     send(msg)
4. Observe: server processes each message (~4000 DB reads + mmr.gen_proof(1000)),
   returns SendBlocksProofV1, issues no ban.
5. After K iterations the server's RocksDB I/O queue and async executor are saturated;
   legitimate peers receive no responses.
```

The unit test at `util/light-client-protocol-server/src/tests/components/get_blocks_proof.rs` L75–78 already provides a minimal reproducible harness: it sends a valid `GetBlocksProof` message and asserts `nc.not_banned(peer_index)`, confirming the complete absence of any throttle or ban on the happy path. Extending this test to send the same message in a loop and measuring server response latency for a concurrent legitimate peer would reproduce the starvation effect. [8](#0-7)

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L5-7)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-95)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = block_hashes
            .into_iter()
            .partition(|block_hash| snapshot.is_main_chain(block_hash));

        let mut positions = Vec::with_capacity(found.len());
        let mut block_headers = Vec::with_capacity(found.len());
        let mut uncles_hash = Vec::with_capacity(found.len());
        let mut extensions = Vec::with_capacity(found.len());

        for block_hash in found {
            let header = snapshot
                .get_block_header(&block_hash)
                .expect("header should be in store");
            positions.push(leaf_index_to_pos(header.number()));
            block_headers.push(header.data());

            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
        }
```

**File:** util/light-client-protocol-server/src/lib.rs (L26-29)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
}
```

**File:** util/light-client-protocol-server/src/lib.rs (L81-91)
```rust
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
```

**File:** util/light-client-protocol-server/src/lib.rs (L207-211)
```rust
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
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

**File:** util/light-client-protocol-server/src/tests/components/get_blocks_proof.rs (L75-78)
```rust
    let peer_index = PeerIndex::new(1);
    protocol.received(nc.context(), peer_index, data).await;

    assert!(nc.not_banned(peer_index));
```
