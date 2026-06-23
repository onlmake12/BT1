The code is clear. Let me confirm the exact behavior of both handlers side by side before rendering the verdict.

**`GetBlocksProofProcess::execute`** — has explicit dedup: [1](#0-0) 

**`GetTransactionsProofProcess::execute`** — no dedup, iterates raw `found` list: [2](#0-1) 

**Limit is 1000 for both:** [3](#0-2) 

**`should_ban` only fires on 4xx status codes:** [4](#0-3) 

Since the transactions handler processes duplicates silently and returns `OK` (200), the peer is never banned.

---

### Title
Missing Duplicate-Hash Deduplication in `GetTransactionsProofProcess::execute` Enables DB Read Amplification — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary
`GetTransactionsProofProcess::execute` does not deduplicate the incoming `tx_hashes` list before performing per-hash DB lookups, unlike `GetBlocksProofProcess::execute` which explicitly rejects duplicate hashes with a `MalformedProtocolMessage` ban. An unprivileged remote peer can send a single `GetTransactionsProof` message containing 1000 copies of the same valid on-chain transaction hash, causing the server to perform 2000 redundant DB reads and build a CBMT proof over 1000 duplicate indices, with no ban or rate-limit consequence.

### Finding Description
`GetBlocksProofProcess::execute` collects all incoming block hashes into a `HashSet` and immediately returns `StatusCode::MalformedProtocolMessage` (a 4xx code that triggers a 5-minute peer ban via `Status::should_ban`) if any duplicate is detected:

```rust
// get_blocks_proof.rs lines 62-70
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

`GetTransactionsProofProcess::execute` has no equivalent guard. After the size check (`> 1000` → reject), it directly partitions the raw iterator:

```rust
// get_transactions_proof.rs lines 54-75
let (found, missing): (Vec<_>, Vec<_>) = self
    .message
    .tx_hashes()
    .to_entity()
    .into_iter()
    .partition(|tx_hash| {
        snapshot
            .get_transaction_info(tx_hash)   // DB read #1 per hash
            ...
    });

for tx_hash in found {
    let (tx, tx_info) = snapshot
        .get_transaction_with_info(&tx_hash)  // DB read #2 per hash
        .expect("tx exists");
    txs_in_blocks
        .entry(tx_info.block_hash)
        .or_insert_with(Vec::new)
        .push((tx, tx_info.index));           // duplicate indices accumulate
}
``` [2](#0-1) 

With 1000 copies of the same valid tx hash:
- **2000 DB reads** are performed (1000 `get_transaction_info` + 1000 `get_transaction_with_info`)
- The `txs_in_blocks` HashMap accumulates 1000 `(tx, same_index)` entries for the same block
- `CBMT::build_merkle_proof` is called with 1000 duplicate indices [5](#0-4) 

Because the handler returns `Status::ok()` (code 200), `should_ban` returns `None` and the peer is never penalized, allowing indefinite repetition. [4](#0-3) 

### Impact Explanation
Each max-size duplicate-hash request amplifies DB reads by ~1000x relative to a legitimate request. An attacker with a single persistent P2P connection can continuously saturate the RocksDB read path of the light-client protocol server. Multiple concurrent connections multiply the effect linearly. The CBMT proof builder also performs unnecessary work over 1000 duplicate indices per request.

### Likelihood Explanation
The light-client protocol is reachable by any unprivileged peer. The message is structurally valid (passes the size check), requires no PoW or stake, and the peer is never banned. The attack is trivially repeatable in a tight loop.

### Recommendation
Add a deduplication check in `GetTransactionsProofProcess::execute` mirroring the pattern already present in `GetBlocksProofProcess::execute`:

```rust
let mut uniq = HashSet::new();
if !tx_hashes.iter().all(|h| uniq.insert(h)) {
    return StatusCode::MalformedProtocolMessage
        .with_context("duplicate tx hash exists");
}
```

This returns a 4xx status, which `should_ban` converts into a 5-minute peer ban, consistent with the blocks-proof handler's behavior. [1](#0-0) 

### Proof of Concept
1. Connect to a CKB node's light-client P2P endpoint.
2. Identify any valid on-chain transaction hash `H`.
3. Build a `GetTransactionsProof` message with `tx_hashes = [H] * 1000` and a valid `last_hash`.
4. Send the message repeatedly in a loop.
5. Observe: the server performs 2000 DB reads per message, returns `SendTransactionsProofV1` (not a ban), and the peer connection remains open.
6. Compare DB read counters with a baseline request of `tx_hashes = [H]` (2 DB reads): the amplification factor is 1000x.

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

**File:** util/light-client-protocol-server/src/constant.rs (L5-7)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
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
