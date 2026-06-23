### Title
Insufficient Cost Bound in `GetLastStateProof` Handler Allows O(N·log H) DB Reads Per Request — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The `GET_LAST_STATE_PROOF_LIMIT` guard only bounds the *count* of difficulty samples, not the *computational cost* of processing them. Each difficulty entry triggers an independent binary search over the full block range, each step issuing two DB reads. An unprivileged peer can craft a valid `GetLastStateProof` message that causes ~46,000 DB reads per request on a mature chain, with no rate limiting and no ban triggered for valid requests.

---

### Finding Description

The guard at the entry point of `execute()` is:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT  // = 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) [2](#0-1) 

With `difficulties.len()=999` and `last_n_blocks=0`, the check evaluates to `999 + 0 = 999 ≤ 1000` and passes.

Each of the 999 difficulties then drives a call to `get_first_block_total_difficulty_is_not_less_than`, which performs a binary search:

```rust
let next_number = (block_less_than_min + block_greater_than_min) / 2;
if let Some(total_difficulty) = self.get_block_total_difficulty(next_number) {
``` [3](#0-2) 

Each call to `get_block_total_difficulty` issues exactly two DB reads:

```rust
fn get_block_total_difficulty(&self, number: BlockNumber) -> Option<U256> {
    self.snapshot
        .get_block_hash(number)
        .and_then(|block_hash| self.snapshot.get_block_ext(&block_hash))
        .map(|block_ext| block_ext.total_difficulty)
}
``` [4](#0-3) 

The `start_block_number` advance optimization:

```rust
if num > start_block_number {
    start_block_number = num - 1;
}
``` [5](#0-4) 

...only helps when found blocks are spread across the chain. In the worst case — 999 difficulties targeting blocks 1, 2, 3, …, 999 at the start of a 10M-height chain — `start_block_number` advances by only 1 per iteration, so each of the 999 binary searches still covers a range of size ~10M, costing O(log 10M) ≈ 23 steps each.

**Total DB reads per request:** 999 × 23 × 2 ≈ **46,000**.

---

### Impact Explanation

A valid `GetLastStateProof` request returns `StatusCode::OK` (200), which does **not** trigger a ban:

```rust
pub fn should_ban(&self) -> Option<Duration> {
    let code = self.code as u16;
    if !(400..500).contains(&code) {
        None
    } else {
        Some(constant::BAD_MESSAGE_BAN_TIME)
    }
}
``` [6](#0-5) 

There is no rate limiting, per-peer request throttling, or concurrency cap anywhere in the light client protocol server.


A single attacker peer can continuously send crafted requests, each causing ~46,000 DB reads, saturating the RocksDB I/O path and degrading the full node's performance for all other operations (block sync, tx relay, RPC).

---

### Likelihood Explanation

- The attacker needs only a valid P2P connection to a node running the light client server.
- The attacker needs to know the chain's total difficulty values for the first ~999 blocks — trivially obtained by syncing the chain or reading public explorers.
- The crafted message passes all existing validation checks (monotonically increasing difficulties, first difficulty > start block's predecessor difficulty, last difficulty < boundary).
- No special privileges, keys, or hashpower are required.

---

### Recommendation

Replace the flat count limit with a cost-aware limit. Options:

1. **Cap total binary search steps**: Track the remaining search range and reject requests where `difficulties.len() × log2(end - start)` exceeds a threshold.
2. **Add per-peer request rate limiting**: Allow at most N `GetLastStateProof` requests per peer per time window.
3. **Replace per-difficulty binary search with a single linear scan**: Walk the block range once, emitting block numbers as each difficulty threshold is crossed — O(H) total but with a single pass and no repeated range traversal.

---

### Proof of Concept

```rust
// Attacker constructs a GetLastStateProof with:
//   last_n_blocks = 0
//   difficulties = [total_diff(block_1), total_diff(block_2), ..., total_diff(block_999)]
//   difficulty_boundary = total_diff(block_1000)
//   last_hash = current tip hash (valid main-chain block)
//   start_number = 0

// Check: 999 + 0*2 = 999 <= 1000  → passes guard
// Each difficulty[i] triggers get_first_block_total_difficulty_is_not_less_than(0, H, diff_i)
// Binary search over [i-1, H) ≈ [0, 10_000_000) → ~23 steps × 2 DB reads = 46 reads
// Total: 999 × 46 ≈ 45,954 DB reads per single message
// No ban issued; attacker repeats indefinitely
``` [7](#0-6)

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L53-54)
```rust
            let next_number = (block_less_than_min + block_greater_than_min) / 2;
            if let Some(total_difficulty) = self.get_block_total_difficulty(next_number) {
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L111-116)
```rust
    fn get_block_total_difficulty(&self, number: BlockNumber) -> Option<U256> {
        self.snapshot
            .get_block_hash(number)
            .and_then(|block_hash| self.snapshot.get_block_ext(&block_hash))
            .map(|block_ext| block_ext.total_difficulty)
    }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
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
