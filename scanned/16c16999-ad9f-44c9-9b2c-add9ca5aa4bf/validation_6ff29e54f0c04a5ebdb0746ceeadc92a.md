Based on my analysis of the code, this is a real vulnerability.

---

### Title
CPU Amplification via Unbounded CBMT Work in `GetTransactionsProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary
The `GetTransactionsProof` handler enforces a limit of 1000 tx_hashes per request, but the actual server-side work is not bounded by that constant. For each distinct block containing a requested transaction, the server loads **all** transactions in that block and hashes them to build a CBMT merkle tree. An unprivileged remote peer can craft a request with 1000 tx_hashes from 1000 different blocks, causing O(1000 × max_txs_per_block) CPU and I/O work per message.

### Finding Description

The limit check in `execute()` only validates the count of requested tx_hashes: [1](#0-0) 

After passing this check, found hashes are grouped by block: [2](#0-1) 

Then, for **each distinct block**, the server fetches the full block and iterates over **all** of its transactions to build the merkle tree: [3](#0-2) 

The first argument to `CBMT::build_merkle_proof` is `block.transactions().iter()...collect()` — every transaction in the block, not just the requested ones. `CBMT` is a Complete Binary Merkle Tree backed by blake2b hashing: [4](#0-3) 

The constant is defined as: [5](#0-4) 

There is no rate limiting, per-peer throttling, or total-work cap anywhere in the protocol handler: [6](#0-5) 

The only ban condition is a malformed message. A well-formed request with 1000 valid tx_hashes from 1000 different blocks is never rejected or throttled.

### Impact Explanation
A single well-formed P2P message causes the server to perform O(1000 × N) blake2b hash operations and O(1000) full block reads from storage, where N is the average transaction count per block. On a chain with blocks containing hundreds of transactions, this is a 100–1000× amplification over what the limit constant implies. Repeated at high frequency from one or more peers, this constitutes a CPU/IO exhaustion DoS.

### Likelihood Explanation
The attacker needs only: (1) a P2P connection to a node running the light client protocol server, and (2) knowledge of valid tx_hashes from different blocks — both trivially obtained from any block explorer or by syncing. No PoW, no keys, no privileged access required.

### Recommendation
Bound the total work, not just the input count. Options:
- Enforce a cap on the total number of distinct blocks (e.g., `txs_in_blocks.len() <= some_block_limit`).
- Enforce a cap on the total number of transactions across all blocks being processed.
- Add per-peer rate limiting on `GetTransactionsProof` messages.

### Proof of Concept
1. Populate (or observe on mainnet) 1000 blocks each containing many transactions.
2. Collect one tx_hash from each block.
3. Send a single `GetTransactionsProof` P2P message with all 1000 hashes.
4. Observe the server executing `CBMT::build_merkle_proof` 1000 times, each iterating over all transactions in its respective block — total work scales as `1000 × txs_per_block`, not `1000`.

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

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
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
