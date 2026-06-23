### Title
Unbounded DB Read Amplification in `GetLastStateProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary

The size guard in `execute` bounds only the *count* of sampled items, not the actual DB work per item. Each of the up to 999 difficulty samples triggers an O(log N) binary-search over the chain, an O(log N) MMR `get_root()` call, and the final `gen_proof` is O(k·log N). Total per-request DB reads are O(k·log N) — not O(k) as the constant name `GET_LAST_STATE_PROOF_LIMIT` implies — and any unprivileged P2P peer can reach this path.

---

### Finding Description

**Entrypoint.** Any connected peer can send a `LightClientMessage::GetLastStateProof` message. The handler is dispatched unconditionally in `try_process`: [1](#0-0) 

**Guard.** The only size check is:

```
difficulties.len() + last_n_blocks * 2 > GET_LAST_STATE_PROOF_LIMIT (1000)
``` [2](#0-1) 

With `difficulties=[d1…d999]` and `last_n_blocks=0`, the check evaluates to `999 ≤ 1000` and passes.

**Phase 1 — difficulty binary search.** `get_block_numbers_via_difficulties` calls `get_first_block_total_difficulty_is_not_less_than` for each difficulty. That function is a binary search over the block range; each step calls `get_block_total_difficulty`, which issues two DB reads (`get_block_hash` + `get_block_ext`). Cost per difficulty: O(log N). Total for 999 difficulties: **O(999 · log N)**. [3](#0-2) 

The `start_block_number` narrowing on line 91 reduces subsequent search ranges, but in the worst case (difficulties clustered near the tip) all 999 searches span the full chain.

**Phase 2 — `complete_headers`.** For each of the 999 resolved block numbers, `complete_headers` calls:

```rust
let mmr = self.snapshot.chain_root_mmr(*number - 1);
mmr.get_root()   // reads O(log N) MMR peak nodes from DB
``` [4](#0-3) 

`chain_root_mmr(n)` creates an MMR of size `leaf_index_to_mmr_size(n)` backed by `&Snapshot`, whose `get_elem` issues one DB read per MMR node: [5](#0-4) 

An MMR with N leaves has O(log N) peaks, so each `get_root()` costs O(log N) DB reads. Total for 999 blocks: **O(999 · log N)**.

**Phase 3 — `reply_proof` / `gen_proof`.** `reply_proof` calls `mmr.gen_proof(items_positions)` with all 999 positions: [6](#0-5) 

Generating a Merkle proof for k positions in an MMR of N leaves requires reading O(k · log N) sibling nodes in the worst case (positions spread across the tree). Total: **O(999 · log N)**.

**Aggregate.** Total DB reads per request ≈ 3 × 999 × log₂(N). For a 10 M-block chain (log₂ ≈ 23): ~69 000 DB reads per single request. There is no per-peer or per-message rate limit visible in the protocol handler.

---

### Impact Explanation

A single crafted request on a 10 M-block chain causes ~69 000 RocksDB point-reads. An attacker maintaining several connections and pipelining requests can multiply this linearly. Because the handler runs on the async executor without back-pressure, sustained flooding can saturate disk I/O and stall block processing on the same node. Impact: **node IO saturation**.

---

### Likelihood Explanation

- No authentication or privilege required — any P2P peer qualifies.
- The message is valid by protocol; no PoW or consensus check applies.
- The attacker only needs to know valid difficulty values (observable from public chain data) and a recent tip hash.
- No rate limiting exists in the handler path.

---

### Recommendation

1. **Bound DB work, not just count.** Replace or supplement the count check with a budget on estimated DB operations (e.g., `difficulties.len() * estimated_log_chain_height ≤ WORK_BUDGET`).
2. **Cache `get_root()` results.** The 999 `chain_root_mmr(n-1).get_root()` calls are independent; a request-scoped cache keyed on block number would reduce Phase 2 to O(999) reads.
3. **Add per-peer request rate limiting** in `try_process` for `GetLastStateProof`.
4. **Single shared MMR for `gen_proof`.** Phase 3 already uses one MMR; ensure positions are deduplicated and sorted before calling `gen_proof` to maximise node sharing.

---

### Proof of Concept

```
# Setup: two nodes, one with 1 000 blocks, one with 10 000 000 blocks.
# Both receive the same GetLastStateProof message:
#   difficulties = [d1, d2, ..., d999]  (999 evenly-spaced total-difficulty values)
#   last_n_blocks = 0
#   last_hash     = current tip hash

# Measure wall-clock time and RocksDB read counters (via Prometheus or perf).
# Expected: latency on the 10M-block node is ~23× higher than on the 1000-block node,
# confirming O(log N) amplification per item.
# Repeat at 10 req/s from a single peer; observe IO wait % on the 10M node.
```

The differential latency test is locally reproducible without any privileged access or majority hashpower.

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L108-112)
```rust
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L73-103)
```rust
    fn get_block_numbers_via_difficulties(
        &self,
        mut start_block_number: BlockNumber,
        end_block_number: BlockNumber,
        difficulties: &[U256],
    ) -> Result<Vec<BlockNumber>, String> {
        let mut numbers = Vec::new();
        let mut current_difficulty = U256::zero();
        for difficulty in difficulties {
            if current_difficulty >= *difficulty {
                continue;
            }
            if let Some((num, diff)) = self.get_first_block_total_difficulty_is_not_less_than(
                start_block_number,
                end_block_number,
                difficulty,
            ) {
                if num > start_block_number {
                    start_block_number = num - 1;
                }
                numbers.push(num);
                current_difficulty = diff;
            } else {
                let errmsg = format!(
                    "the difficulty ({difficulty:#x}) is not in the block range [{start_block_number}, {end_block_number})"
                );
                return Err(errmsg);
            }
        }
        Ok(numbers)
    }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L153-163)
```rust
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

**File:** util/snapshot/src/lib.rs (L293-296)
```rust
impl MMRStore<HeaderDigest> for &Snapshot {
    fn get_elem(&self, pos: u64) -> MMRResult<Option<HeaderDigest>> {
        Ok(self.store.get_header_digest(pos))
    }
```
