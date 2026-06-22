Based on my investigation of the code, here is my assessment:

---

### Title
Unbounded `GetBlocksProof` Request Rate with O(k·log(N)) MMR DB Reads Enables Low-Rate DoS on Light Client Servers — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`, `util/light-client-protocol-server/src/lib.rs`)

### Summary

An unprivileged remote peer can repeatedly send `GetBlocksProof` messages containing up to 1000 valid main-chain block hashes. The server performs O(k·log(N)) RocksDB reads per request via `mmr.gen_proof(items_positions)`, where k ≤ 1000 and N is the chain height. The `LightClientProtocol` handler has **no rate limiter**, allowing an attacker to saturate the server's I/O at minimal network cost.

### Finding Description

**Entry point:** Any peer connecting to the light client protocol can send a `GetBlocksProof` message. The `received` handler in `LightClientProtocol` dispatches directly to `try_process` with no rate check. [1](#0-0) 

The `LightClientProtocol` struct contains only `shared: Shared` — no `rate_limiter` field exists, unlike `Relayer` which explicitly has one. [2](#0-1) 

The per-request limit is 1000 hashes: [3](#0-2) 

After validating hashes are on the main chain, `execute()` calls `reply_proof` with up to 1000 MMR positions: [4](#0-3) 

Inside `reply_proof`, `mmr.gen_proof(items_positions)` is called on an MMR backed by RocksDB reads via `get_header_digest`: [5](#0-4) 

The `MMRStore` implementation for `&Snapshot` issues one RocksDB read per MMR node accessed: [6](#0-5) 

Standard MMR proof generation for k leaves in a tree of N leaves requires O(k·log(N)) node reads. On a mainnet node with ~10M blocks, log₂(10M) ≈ 23, yielding ~23,000 RocksDB reads per max-size request.

### Impact Explanation

With no rate limiting on the light client protocol, an attacker can send `GetBlocksProof` messages at line rate. Each request with 1000 spread-out valid hashes forces ~23,000 RocksDB reads. At even 10 requests/second, this is ~230,000 RocksDB reads/second from a single peer — enough to saturate I/O on a typical server and degrade or block normal chain processing. The cost grows with chain height (log factor), making the attack progressively cheaper relative to server cost as the chain matures.

### Likelihood Explanation

The attack requires only a valid P2P connection and knowledge of 1000 main-chain block hashes (trivially obtained from any public block explorer or by syncing headers). No PoW, no keys, no privileged access needed.

### Recommendation

Add a per-peer rate limiter to `LightClientProtocol` analogous to the one in `Relayer`: [7](#0-6) 

Additionally, consider reducing `GET_BLOCKS_PROOF_LIMIT` or adding a global request queue with backpressure.

### Proof of Concept

1. Connect to a mainnet light client server via P2P.
2. Obtain 1000 valid main-chain block hashes spread across the full chain height range (e.g., blocks 0, 10000, 20000, …, 9,990,000).
3. Repeatedly send `GetBlocksProof { last_hash: <tip>, block_hashes: [1000 hashes] }` in a tight loop.
4. Observe server RocksDB I/O saturating; benchmark `gen_proof(1000 positions)` on chains of height 100, 10,000, 1,000,000 and confirm cost grows with log(N).

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-35)
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
```

**File:** util/light-client-protocol-server/src/lib.rs (L55-80)
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

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L104-113)
```rust
        self.protocol
            .reply_proof::<packed::SendBlocksProofV1>(
                self.peer,
                self.nc,
                &last_block,
                positions,
                proved_items,
                missing_items,
            )
            .await
```

**File:** util/snapshot/src/lib.rs (L293-296)
```rust
impl MMRStore<HeaderDigest> for &Snapshot {
    fn get_elem(&self, pos: u64) -> MMRResult<Option<HeaderDigest>> {
        Ok(self.store.get_header_digest(pos))
    }
```

**File:** sync/src/relayer/mod.rs (L84-123)
```rust
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
    }

    /// Get shared state
    pub fn shared(&self) -> &Arc<SyncShared> {
        &self.shared
    }

    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::RelayMessageUnionReader<'_>,
    ) -> Status {
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```
