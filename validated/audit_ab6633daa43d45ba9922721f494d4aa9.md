### Title
Unbounded `last_n_numbers` Vector via Minimum `difficulty_boundary` Bypasses `GET_LAST_STATE_PROOF_LIMIT` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary

`GetLastStateProofProcess::execute` enforces a size limit using `last_n_blocks` as a proxy for server work, but the actual work is determined by `difficulty_boundary_block_number`, which is computed from the attacker-controlled `difficulty_boundary` field. When `difficulty_boundary=1`, the binary search resolves to block 0 immediately (O(1)), `difficulty_boundary_block_number` is set to 0, and `last_n_numbers` becomes `(0..last_block_number)` — a vector of millions of entries — completely bypassing the `GET_LAST_STATE_PROOF_LIMIT=1000` guard. Each entry then triggers O(log N) RocksDB reads and an MMR root computation in `complete_headers`.

### Finding Description

**Limit check (lines 201–205):**

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT  // = 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

With `difficulties=[]` and `last_n_blocks=499`: `0 + 499×2 = 998 ≤ 1000` → passes. [1](#0-0) [2](#0-1) 

**Binary search short-circuits at block 0 (lines 30–33):**

```rust
if let Some(start_total_difficulty) = self.get_block_total_difficulty(start_block_number) {
    if start_total_difficulty >= *min_total_difficulty {
        return Some((start_block_number, start_total_difficulty));
    }
}
```

With `start_block_number=0` and `min_total_difficulty=1`, genesis total difficulty is always ≥ 1, so the function returns `(0, genesis_difficulty)` after exactly **2 RocksDB reads** — O(1), not O(log N). [3](#0-2) 

**Adjustment guard does not fire (lines 313–316):**

```rust
if last_block_number - difficulty_boundary_block_number < last_n_blocks {
    difficulty_boundary_block_number = last_block_number - last_n_blocks;
}
```

With `difficulty_boundary_block_number=0` and `last_block_number` in the millions, `last_block_number - 0 < 499` is false, so `difficulty_boundary_block_number` stays at 0. [4](#0-3) 

**Unbounded `last_n_numbers` (lines 318–319):**

```rust
let last_n_numbers =
    (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
```

This produces a vector of `last_block_number` entries (e.g., 10,000,000 on mainnet), not `last_n_blocks=499`. [5](#0-4) 

**`complete_headers` iterates all entries (lines 132–177):**

For each of the millions of block numbers, the server performs:
- `snapshot.get_ancestor(last_hash, number)` — O(log N) RocksDB reads
- `snapshot.get_block(hash)` — additional RocksDB reads
- `snapshot.chain_root_mmr(number - 1).get_root()` — O(log N) MMR computation [6](#0-5) 

**No rate limiting exists** in the light client protocol handler — no per-peer throttle, no concurrent request cap, no cooldown. [7](#0-6) 

### Impact Explanation

A single attacker peer sends one `GetLastStateProof` message (~200 bytes) and forces the server to perform O(N × log N) RocksDB reads and MMR computations where N = chain height. On a mainnet node with ~10M blocks, this is tens of millions of I/O operations per request. Repeated at any rate, this saturates the node's I/O and CPU, preventing it from processing new blocks, serving legitimate peers, or maintaining sync — constituting CKB network congestion with negligible attacker cost.

### Likelihood Explanation

The attack requires only a valid P2P connection to a light-client-enabled CKB node. No PoW, no stake, no key, no privilege. The crafted message is trivially constructable: `start_number=0`, `last_hash=tip` (obtained from `GetLastState`), `difficulty_boundary=1`, `last_n_blocks=499`, `difficulties=[]`. The limit check is arithmetically satisfied. The path is fully deterministic and locally testable.

### Recommendation

Replace the proxy-based limit check with a direct bound on the actual output size. After computing `difficulty_boundary_block_number`, enforce:

```rust
if last_block_number - difficulty_boundary_block_number > last_n_blocks {
    return StatusCode::InvalidRequest.with_context("difficulty_boundary resolves too early");
}
```

Or, equivalently, validate that `difficulty_boundary` corresponds to a block number no earlier than `last_block_number - last_n_blocks` before performing any further computation.

### Proof of Concept

```
GetLastStateProof {
    last_hash:           <current tip hash>,
    start_hash:          <genesis hash>,
    start_number:        0,
    last_n_blocks:       499,
    difficulty_boundary: U256::from(1u64),
    difficulties:        [],
}
```

1. Limit check: `0 + 499×2 = 998 ≤ 1000` → passes.
2. `last_block_number - 0 > 499` → enters `else` branch.
3. Binary search returns `(0, genesis_difficulty)` after 2 reads.
4. `last_block_number - 0 ≥ 499` → no adjustment.
5. `last_n_numbers = (0..last_block_number)` → ~10M entries on mainnet.
6. `complete_headers` performs ~10M × O(log N) RocksDB + MMR operations.
7. Node I/O saturated; repeat from step 1 with a new connection.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L30-33)
```rust
        if let Some(start_total_difficulty) = self.get_block_total_difficulty(start_block_number) {
            if start_total_difficulty >= *min_total_difficulty {
                return Some((start_block_number, start_total_difficulty));
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-163)
```rust
        for number in numbers {
            if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
                let position = leaf_index_to_pos(*number);
                positions.push(position);

                let ancestor_block = self
                    .snapshot
                    .get_block(&ancestor_header.hash())
                    .ok_or_else(|| {
                        format!(
                            "failed to find block for header#{} (hash: {:#x})",
                            number,
                            ancestor_header.hash()
                        )
                    })?;
                let uncles_hash = ancestor_block.calc_uncles_hash();
                let extension = ancestor_block.extension();

                let parent_chain_root = if *number == 0 {
                    Default::default()
                } else {
                    let mmr = self.snapshot.chain_root_mmr(*number - 1);
                    match mmr.get_root() {
                        Ok(root) => root,
                        Err(err) => {
                            let errmsg = format!(
                                "failed to generate a root for block#{number} since {err:?}"
                            );
                            return Err(errmsg);
                        }
                    }
                };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L313-316)
```rust
            if last_block_number - difficulty_boundary_block_number < last_n_blocks {
                // There is not enough blocks after the difficulty boundary, so we take more.
                difficulty_boundary_block_number = last_block_number - last_n_blocks;
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L318-319)
```rust
            let last_n_numbers =
                (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
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
