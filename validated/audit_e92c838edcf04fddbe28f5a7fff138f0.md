Audit Report

## Title
Missing Deduplication Guard in `GetTransactionsProofProcess::execute` Enables Unbounded Work Amplification — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary

`GetTransactionsProofProcess::execute` performs no deduplication on the incoming `tx_hashes` list, unlike `GetBlocksProofProcess::execute` which explicitly rejects duplicate hashes with a `MalformedProtocolMessage` status. An unprivileged remote peer can send a `GetTransactionsProof` message with up to 1000 identical confirmed transaction hashes, forcing the server to perform 2000 database reads, CBMT proof construction over 1000 identical indices, and response serialization of 1000 copies of the same transaction — with no ban applied and no rate limiter on the LightClient handler.

## Finding Description

`GetBlocksProofProcess::execute` explicitly deduplicates its input at lines 62–70:

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

`GetTransactionsProofProcess::execute` has no equivalent guard. The only input validation is an empty check and a count ceiling of 1000: [2](#0-1) 

With 1000 identical confirmed tx hashes, the full execution path proceeds:

1. **Partition (L54–64):** Iterates all 1000 hashes, calling `snapshot.get_transaction_info(tx_hash)` and `snapshot.is_main_chain(...)` for each — 1000 DB reads. [3](#0-2) 

2. **HashMap build (L66–75):** Calls `snapshot.get_transaction_with_info(&tx_hash)` for each of the 1000 hashes — 1000 more DB reads. All 1000 `(tx, index)` pairs land in the same block's `Vec`. [4](#0-3) 

3. **CBMT proof (L86–97):** `CBMT::build_merkle_proof` is called with a `Vec` of 1000 identical `u32` indices. The `.expect()` call panics if CBMT returns `None` for degenerate duplicate-index input, which would crash the node. [5](#0-4) 

4. **Response serialization (L99–114):** A `FilteredBlock` is built containing 1000 copies of the same transaction data, then serialized and sent. [6](#0-5) 

The `LightClientProtocol` handler only bans a peer when `status.should_ban()` returns `Some(...)`, which requires a 4xx status code. Since no `MalformedProtocolMessage` is returned for duplicate tx hashes, no ban is triggered: [7](#0-6) 

`should_ban()` only returns `Some` for status codes in the 400–499 range: [8](#0-7) 

Unlike the `Relayer` protocol which has a per-peer rate limiter keyed by `(PeerIndex, message_type)`, `LightClientProtocol` has no rate limiting at all: [9](#0-8) [10](#0-9) 

## Impact Explanation

This is a **High** severity issue matching: *"Vulnerabilities which could easily crash a CKB node"* and *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

A single attacker connection can sustain 2000 DB reads and a large serialized response per request, repeated indefinitely without ban. The `.expect()` on `CBMT::build_merkle_proof` with 1000 identical indices may also panic and crash the node process. Under sustained attack from even a single peer, this can exhaust the node's I/O capacity and degrade or crash the LightClient-serving node.

## Likelihood Explanation

The attack requires only an open LightClient P2P session (publicly available) and knowledge of one confirmed on-chain transaction hash (trivially obtainable from any block explorer). No privileged access, hashpower, or key material is needed. The message is well-formed and passes all existing validation. The attacker is never banned, so the attack can be sustained indefinitely from a single connection.

## Recommendation

Add a deduplication check immediately after the count check in `GetTransactionsProofProcess::execute`, mirroring `GetBlocksProofProcess`:

```rust
let tx_hashes_vec: Vec<_> = self.message.tx_hashes().to_entity().into_iter().collect();
let mut uniq = HashSet::new();
if !tx_hashes_vec.iter().all(|h| uniq.insert(h)) {
    return StatusCode::MalformedProtocolMessage
        .with_context("duplicate tx hash exists");
}
```

This returns a 4xx status, triggering `should_ban()` and a peer ban via `nc.ban_peer(...)`. Additionally, consider adding a per-peer rate limiter to `LightClientProtocol` consistent with the approach used in `Relayer`.

## Proof of Concept

1. Connect to a CKB node with the LightClient protocol enabled.
2. Obtain any confirmed on-chain transaction hash `h` (e.g., from a block explorer or `get_transaction` RPC).
3. Obtain the current tip hash `tip_hash`.
4. Send a `GetTransactionsProof { last_hash: tip_hash, tx_hashes: [h; 1000] }` message over the LightClient P2P session.
5. Observe the server performs 2000 DB reads (1000 × `get_transaction_info` + 1000 × `get_transaction_with_info`) and either returns a `SendTransactionsProof` response containing 1000 copies of the same transaction, or panics at the `.expect()` on `CBMT::build_merkle_proof` with duplicate indices.
6. Confirm no ban is applied to the sending peer.
7. Repeat in a tight loop and measure server CPU, I/O, and outbound bandwidth vs. a single-hash request to confirm the O(N) amplification ratio.

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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L33-39)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L99-114)
```rust
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

**File:** util/light-client-protocol-server/src/lib.rs (L81-86)
```rust
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
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

**File:** sync/src/relayer/mod.rs (L63-98)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

#[derive(Debug, Eq, PartialEq)]
pub enum ReconstructionResult {
    Block(BlockView),
    Missing(Vec<usize>, Vec<usize>),
    Collided,
    Error(Status),
}

/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
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

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
```
