Audit Report

## Title
CPU/IO Amplification via Unbounded Per-Block Work in `GetTransactionsProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary
The `GetTransactionsProof` handler caps the number of requested tx_hashes at 1000, but the actual server-side work is proportional to `distinct_blocks × transactions_per_block`, not to the number of requested hashes. An unprivileged P2P peer can send a single well-formed message with 1000 tx_hashes drawn from 1000 different blocks, forcing the server to load 1000 full blocks and run `CBMT::build_merkle_proof` (blake2b-based) plus `calc_witnesses_root` over every transaction in each block. There is no rate limiting on the `LightClientProtocol` handler, so this can be repeated at will.

## Finding Description

**Input validation** only checks the count of requested hashes: [1](#0-0) 

**Grouping by block** — up to 1000 distinct blocks, one hash each: [2](#0-1) 

**Per-block work** — for each of the up to 1000 blocks, the server fetches the full block and passes **all** of its transactions to `CBMT::build_merkle_proof`, then calls `calc_witnesses_root` (also O(N) over all transactions): [3](#0-2) [4](#0-3) 

Each `CBMT` merge node invokes a fresh blake2b context: [5](#0-4) 

**No rate limiting exists** in `LightClientProtocol`. The `received` handler dispatches directly to `try_process` with no per-peer or per-message-type quota: [6](#0-5) 

By contrast, the `Relayer` protocol explicitly maintains a `rate_limiter` and checks it before every message dispatch. `LightClientProtocol` has no equivalent field or check. [7](#0-6) 

The only ban condition is a structurally malformed message. A well-formed request with 1000 valid tx_hashes from 1000 different blocks is never rejected or throttled. [8](#0-7) 

## Impact Explanation
A single P2P message triggers O(1000 × N) blake2b hash operations and O(1000) full block reads from storage, where N is the number of transactions per block. On a chain with blocks containing hundreds of transactions this is a 100–1000× amplification over what the limit constant implies. Sent continuously from one or more peers, this constitutes CPU and I/O exhaustion capable of crashing or severely degrading a CKB node. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node** and **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
The attacker requires only: (1) a P2P connection to a node with the light client protocol server enabled, and (2) a list of valid tx_hashes from distinct blocks — trivially obtained from any block explorer or by syncing. No proof-of-work, no keys, no privileged access, and no victim mistakes are required. The attack is fully repeatable and can be parallelized across multiple connections.

## Recommendation
Bound total work, not just input count. Concrete options:
- Add a cap on the number of distinct blocks: `if txs_in_blocks.len() > MAX_BLOCKS_PER_PROOF { return error; }` with a small constant (e.g., 50).
- Add a per-peer rate limiter on `LightClientProtocol` mirroring the one already present in `Relayer` (`governor::RateLimiter` keyed by `(PeerIndex, message_item_id)`).
- Combine both: a per-message rate limit and a distinct-block cap.

## Proof of Concept
1. Sync or observe mainnet/testnet to collect one tx_hash from each of 1000 different blocks.
2. Obtain the current tip hash as `last_hash`.
3. Construct a `GetTransactionsProof` molecule message with all 1000 hashes and send it over a P2P connection to a node running `LightClientProtocol`.
4. Observe the server executing `CBMT::build_merkle_proof` and `calc_witnesses_root` 1000 times, each iterating over all transactions in its respective block.
5. Repeat in a tight loop (or from multiple peers) to saturate CPU and storage I/O.

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L82-97)
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
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L104-106)
```rust
            let filtered_block = packed::FilteredBlock::new_builder()
                .header(block.header().data())
                .witnesses_root(block.calc_witnesses_root())
```

**File:** util/types/src/utilities/merkle_tree.rs (L9-19)
```rust
impl Merge for MergeByte32 {
    type Item = Byte32;
    fn merge(left: &Self::Item, right: &Self::Item) -> Self::Item {
        let mut ret = [0u8; 32];
        let mut blake2b = new_blake2b();

        blake2b.update(left.as_slice());
        blake2b.update(right.as_slice());
        blake2b.finalize(&mut ret);
        ret.into()
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

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```
