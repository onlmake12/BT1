All code claims check out against the actual source. Here is the validated report:

---

Audit Report

## Title
Unbounded Per-Message DB Amplification in `GetTransactionsProofProcess::execute` with No Rate Limiting — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary
The `GetTransactionsProof` handler allows up to 1000 tx hashes per message and performs O(6000 + 1000×log(chain_length)) RocksDB reads per request. The `LightClientProtocol` struct carries no rate limiter, unlike the relay protocol which enforces a per-peer `RateLimiter`. A successful request returns `Status::ok()` (code 200), which never triggers `should_ban()`, so an attacker can flood the server with maximum-size requests indefinitely with no consequence.

## Finding Description
`GET_TRANSACTIONS_PROOF_LIMIT` is set to 1000 and the check is strictly `> 1000`, so exactly 1000 hashes pass through. [1](#0-0) [2](#0-1) 

For each of the 1000 hashes, `get_transaction_info()` is called in the partition step, then `get_transaction_with_info()` for each found tx. [3](#0-2) 

For each unique block (up to 1000), `get_block()`, `CBMT::build_merkle_proof()`, `get_block_uncles()`, and `get_block_extension()` are all called. [4](#0-3) 

`reply_proof()` then calls `mmr.get_root()` and `mmr.gen_proof(items_positions)` where `items_positions` can hold up to 1000 entries, costing O(k × log N) MMR node reads on a chain of length N. [5](#0-4) 

On success the handler returns `Status::ok()` (code 200). `should_ban()` only fires on 4xx codes, so no ban is ever triggered. [6](#0-5) 

`LightClientProtocol` carries only `shared: Shared` — no rate limiter field exists. [7](#0-6) 

`received()` dispatches directly to `try_process()` with no throttle. [8](#0-7) 

By contrast, the relay protocol explicitly constructs a per-peer `RateLimiter` keyed by `(PeerIndex, message_type)` at 30 req/s and checks it before processing. [9](#0-8) 

## Impact Explanation
A single attacker peer, with no authentication, can continuously send `GetTransactionsProof` messages each triggering ~6000 direct DB reads plus ~20,000 MMR node reads (on a 1M-block chain). With no rate limiting and no ban, this saturates RocksDB I/O and degrades or crashes the node for all other peers. This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion / easily crash a CKB node with few costs* (10001–15000 points).

## Likelihood Explanation
The attack requires only: (1) a standard P2P connection to a node with light client protocol enabled — no authentication; (2) 1000 valid on-chain tx hashes, trivially obtained by scanning public block data (e.g., cellbase txs); (3) the current tip hash, which is publicly observable. The attack is fully repeatable in a tight loop from a single connection.

## Recommendation
1. Add a per-peer rate limiter to `LightClientProtocol`, mirroring the `RateLimiter<(PeerIndex, u32)>` used in `sync/src/relayer/mod.rs` lines 81–92, and check it at the top of `received()` before dispatching to `try_process()`.
2. Reduce `GET_TRANSACTIONS_PROOF_LIMIT` (currently 1000 in `util/light-client-protocol-server/src/constant.rs` line 7) to a value that bounds worst-case DB work to an acceptable level (e.g., 100–200).
3. Consider banning peers that repeatedly hit the rate limit, tracking request rate per peer.

## Proof of Concept
```
1. Run a CKB full node with light client protocol enabled on a chain with ≥1000 blocks.
2. Collect 1000 tx hashes from 1000 distinct blocks (e.g., cellbase txs from block explorer).
3. Obtain the current tip hash (publicly observable).
4. Construct a GetTransactionsProof message:
     last_hash = <tip_hash>
     tx_hashes = [hash_0, ..., hash_999]   // 1000 entries across 1000 distinct blocks
5. Send the message repeatedly in a tight loop from a single peer connection.
6. Monitor RocksDB read counters (rocksdb.number.db.get) — each message produces
   O(6000 + 1000×log(chain_len)) reads with no ban and no throttle.
7. Observe node I/O saturation and degraded response times for other peers.
```

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L54-75)
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L82-126)
```rust
        for (block_hash, txs_and_tx_indices) in txs_in_blocks.into_iter() {
            let block = snapshot
                .get_block(&block_hash)
                .expect("block should be in store");
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

            let txs: Vec<_> = txs_and_tx_indices
                .into_iter()
                .map(|(tx, _)| tx.data())
                .collect();

            let filtered_block = packed::FilteredBlock::new_builder()
                .header(block.header().data())
                .witnesses_root(block.calc_witnesses_root())
                .transactions(txs)
                .proof(
                    packed::MerkleProof::new_builder()
                        .indices(merkle_proof.indices().as_ref())
                        .lemmas(merkle_proof.lemmas().to_owned())
                        .build(),
                )
                .build();

            positions.push(leaf_index_to_pos(block.number()));
            filtered_blocks.push(filtered_block);

            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
        }
```

**File:** util/light-client-protocol-server/src/lib.rs (L26-36)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
}

impl LightClientProtocol {
    /// Create a new light client protocol handler.
    pub fn new(shared: Shared) -> Self {
        Self { shared }
    }
}
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

**File:** util/light-client-protocol-server/src/lib.rs (L199-216)
```rust
            let mmr = snapshot.chain_root_mmr(last_block.number() - 1);
            let parent_chain_root = match mmr.get_root() {
                Ok(root) => root,
                Err(err) => {
                    let errmsg = format!("failed to generate a root since {err:?}");
                    return StatusCode::InternalError.with_context(errmsg);
                }
            };
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
                        let errmsg = format!("failed to generate a proof since {err:?}");
                        return StatusCode::InternalError.with_context(errmsg);
                    }
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

**File:** sync/src/relayer/mod.rs (L81-92)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```
