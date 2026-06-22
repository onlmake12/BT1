Based on my thorough code analysis, here is my assessment:

---

### Title
Unbounded CPU Amplification via `GetTransactionsProof` with No Rate Limiting — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary

Any unprivileged P2P peer can send a `GetTransactionsProof` message with up to 1000 tx_hashes all belonging to the same large block, forcing the server to perform O(N log N) CBMT proof construction and O(N) witness root computation per request, with zero rate limiting in the light client protocol handler.

### Finding Description

**Entrypoint**: `LightClientProtocol::received` in `lib.rs` dispatches any `GetTransactionsProof` P2P message directly to `GetTransactionsProofProcess::execute` with no per-peer throttle. [1](#0-0) 

**No rate limiting exists anywhere in the light client server**: A grep across the entire `util/light-client-protocol-server/` directory finds zero occurrences of `rate_limit`, `rate_limiter`, or `throttle`. Compare this with the hole-punching protocol, which explicitly checks `self.rate_limiter.check_key(...)` before processing. [2](#0-1) 

**The only guard is a count check** (`> 1000` → reject), so exactly 1000 tx_hashes are accepted: [3](#0-2) 

**When all 1000 hashes resolve to the same block**, `txs_in_blocks` has one entry with 1000 `(tx, index)` pairs. The loop then calls:

1. **`CBMT::build_merkle_proof`** with ALL transactions in the block as leaves (not just the requested ones). For a block with B transactions, this builds the full CBMT — O(B) tree construction + O(K log B) proof path traversal for K=1000 requested indices = O(N log N) total: [4](#0-3) 

2. **`block.calc_witnesses_root()`** — computes `merkle_root(&self.tx_witness_hashes[..])`, another O(B) pass over all witness hashes in the block: [5](#0-4) [6](#0-5) 

3. **`mmr.gen_proof(positions)`** in `reply_proof` — with one block, this is O(log chain_height), not the dominant cost: [7](#0-6) 

**CBMT is from the `merkle-cbt` external crate** (`ExCBMT`), aliased as `CBMT` in `merkle_tree.rs`: [8](#0-7) 

### Impact Explanation

A sustained flood of `GetTransactionsProof` messages — each with 1000 tx_hashes from the same large block — forces the server to repeatedly perform O(N log N) blake2b hash operations with no throttle. This can saturate CPU on the full node, degrading or crashing the light client serving capability and potentially the node's overall responsiveness.

### Likelihood Explanation

- All transaction hashes are public on-chain data; an attacker can trivially collect 1000 hashes from any large block.
- CKB mainnet blocks regularly contain hundreds of transactions within the consensus `max_block_bytes` limit.
- The attacker needs only a single P2P connection — no privileged role, no PoW, no key material.
- The attack is repeatable indefinitely since no ban or rate limit is triggered by a well-formed request.

### Recommendation

Add per-peer rate limiting to `LightClientProtocol::received` (or inside `try_process`) for `GetTransactionsProof` messages, analogous to the `rate_limiter.check_key(...)` guard used in the hole-punching protocol. Additionally, consider capping the number of tx_hashes that may resolve to a single block within one request.

### Proof of Concept

1. Identify a block on the main chain with ≥1000 transactions; collect 1000 of its tx_hashes (all public).
2. Connect as a peer to a CKB full node with the light client protocol enabled.
3. In a tight loop, send `GetTransactionsProof { last_hash: <tip>, tx_hashes: [1000 hashes from same block] }`.
4. Observe: each message triggers `CBMT::build_merkle_proof` over 1000 leaves + `calc_witnesses_root` over 1000 witness hashes, with no throttle. CPU usage climbs monotonically; node responsiveness degrades.

### Citations

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

**File:** util/light-client-protocol-server/src/lib.rs (L207-217)
```rust
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
            };
```

**File:** util/light-client-protocol-server/src/constant.rs (L1-7)
```rust
use std::time::Duration;

pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);

pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L104-114)
```rust
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

**File:** util/types/src/core/views.rs (L775-777)
```rust
    pub fn calc_witnesses_root(&self) -> packed::Byte32 {
        merkle_root(&self.tx_witness_hashes[..])
    }
```

**File:** util/types/src/utilities/merkle_tree.rs (L2-23)
```rust
use merkle_cbt::{CBMT as ExCBMT, MerkleProof as ExMerkleProof, merkle_tree::Merge};

use crate::{packed::Byte32, prelude::*};

/// Merge function for computing Merkle tree nodes from pairs of `Byte32` values.
pub struct MergeByte32;

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
}

/// Complete Binary Merkle Tree specialized for `Byte32` leaves.
pub type CBMT = ExCBMT<Byte32, MergeByte32>;
```
