Audit Report

## Title
Missing `pending_compact_blocks` Guard in `GetBlockTransactionsProcess::execute` Enables Amplified Resource Exhaustion — (`sync/src/relayer/get_block_transactions_process.rs`)

## Summary

`GetBlockTransactionsProcess::execute` unconditionally queries the persistent chain store for any block hash supplied by a remote peer, with no check that the hash corresponds to an active compact-block reconstruction. Any unprivileged peer can send `GetBlockTransactions` for arbitrary committed blocks at the rate-limiter ceiling, forcing repeated disk reads, serialization, and large outbound sends. The receiver-side handler (`BlockTransactionsProcess`) enforces the `pending_compact_blocks` invariant; the sender-side handler does not.

## Finding Description

In `sync/src/relayer/get_block_transactions_process.rs`, the `execute` method performs two input guards — a count check against `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) and a `max_uncles_num()` check — then immediately queries the persistent store: [1](#0-0) [2](#0-1) 

There is no check that `block_hash` exists in `pending_compact_blocks`. By contrast, `BlockTransactionsProcess::execute` gates all processing on an `Entry::Occupied` lookup: [3](#0-2) 

The rate limiter in `Relayer::try_process` is keyed by `(PeerIndex, message_item_id)` at 30 req/s per peer: [4](#0-3) 

With `MAX_RELAY_PEERS = 128` and `MAX_RELAY_TXS_NUM_PER_BATCH = 32767`: [5](#0-4) 

An attacker controlling 128 connections sustains 3,840 `GetBlockTransactions` requests/second, each triggering a full block read from disk, collection of up to 32,767 transactions, serialization, and outbound send. The request payload is small and fixed-size; the response is proportional to the full block size — a classic amplification pattern.

The integration test confirms this path is reachable and produces a real response for a committed (non-pending) block: [6](#0-5) 

The block is submitted via `node.submit_block()` (committed to the chain store) and then queried directly — no compact-block reconstruction is in progress.

## Impact Explanation

This is a **High** severity issue matching: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

The request/response asymmetry is the core amplifier: a peer sends a small, fixed-size `GetBlockTransactions` message and forces the node to perform a full disk read of a historical block, collect and serialize up to 32,767 transactions, and transmit the result. Targeting epoch-boundary or high-throughput blocks maximizes per-request cost. At 3,840 req/s across 128 peers, the node's disk I/O subsystem, relay I/O thread, and outbound bandwidth can be saturated, degrading or crashing the node and its ability to participate in block relay for the broader network.

## Likelihood Explanation

Any peer that can establish a P2P connection can execute this. Block hashes for committed blocks are publicly available from any chain explorer or synced node. No key material, hashpower, or operator access is required. The default `max_peers = 125` (117 inbound slots) means an attacker can fill nearly all inbound connections. The attack is repeatable, cheap for the attacker (small outbound messages, large inbound responses), and requires no coordination. [7](#0-6) 

## Recommendation

Add a guard at the top of `execute` (after the existing input checks) that verifies `block_hash` is present in `pending_compact_blocks` before querying the store. If the hash is not pending, return `Status::ok()` silently, consistent with how `BlockTransactionsProcess` handles unknown hashes:

```rust
let is_pending = shared
    .state()
    .pending_compact_blocks()
    .await
    .contains_key(&block_hash);
if !is_pending {
    return Status::ok();
}
```

This aligns the sender-side guard with the receiver-side invariant already enforced in `BlockTransactionsProcess::execute`.

## Proof of Concept

1. Connect to a non-IBD CKB node as a relay peer (requires only a valid P2P handshake).
2. Obtain the hash of any committed block (e.g., genesis block hash, publicly known from any explorer).
3. Send a `RelayMessage` containing `GetBlockTransactions { block_hash: <committed_hash>, indexes: [0..N-1], uncle_indexes: [] }` where N ≤ 32,767.
4. Observe the node responds with a `BlockTransactions` message containing full transaction data — confirmed by the existing integration test `MissingUncleRequest` which does exactly this with a committed block.
5. Repeat at 30 req/s per connection; open up to 117 additional inbound connections to multiply throughput to ~3,510 req/s.
6. Assert: no `pending_compact_blocks` entry for the queried hash exists at any point, yet the node serves the full response — confirming the missing guard.

### Citations

**File:** sync/src/relayer/get_block_transactions_process.rs (L37-50)
```rust
            if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "Indexes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    get_block_transactions.indexes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
            if get_block_transactions.uncle_indexes().len() > shared.consensus().max_uncles_num() {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "UncleIndexes count({}) > consensus max_uncles_num({})",
                    get_block_transactions.uncle_indexes().len(),
                    shared.consensus().max_uncles_num(),
                ));
            }
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L60-97)
```rust
        if let Some(block) = shared.store().get_block(&block_hash) {
            let transactions = self
                .message
                .indexes()
                .iter()
                .filter_map(|i| {
                    block
                        .transactions()
                        .get(Into::<u32>::into(i) as usize)
                        .cloned()
                })
                .collect::<Vec<_>>();

            let uncles = self
                .message
                .uncle_indexes()
                .iter()
                .filter_map(|i| block.uncles().get(Into::<u32>::into(i) as usize))
                .collect::<Vec<_>>();

            let content = packed::BlockTransactions::new_builder()
                .block_hash(block_hash)
                .transactions(
                    transactions
                        .into_iter()
                        .map(|tx| tx.data())
                        .collect::<Vec<_>>(),
                )
                .uncles(
                    uncles
                        .into_iter()
                        .map(|uncle| uncle.data())
                        .collect::<Vec<_>>(),
                )
                .build();
            let message = packed::RelayMessage::new_builder().set(content).build();

            return async_send_message_to(&self.nc, self.peer, &message).await;
```

**File:** sync/src/relayer/block_transactions_process.rs (L65-70)
```rust
        if let Entry::Occupied(mut pending) = shared
            .state()
            .pending_compact_blocks()
            .await
            .entry(block_hash.clone())
        {
```

**File:** sync/src/relayer/mod.rs (L59-61)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/mod.rs (L88-99)
```rust
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
```

**File:** test/src/specs/relay/get_block_transactions_process.rs (L24-49)
```rust
        node.mine(1);

        let builder = node.new_block_builder(None, None, None);
        let block1 = builder.clone().nonce(0).build();
        let block2 = builder.nonce(1).build();
        node.submit_block(&block1);
        node.submit_block(&block2);

        let builder = node.new_block_builder(None, None, None);
        let block = builder.set_uncles(vec![block2.as_uncle()]).nonce(0).build();
        node.submit_block(&block);

        let content = packed::GetBlockTransactions::new_builder()
            .block_hash(block.hash())
            .uncle_indexes([0u32])
            .build();
        let message = packed::RelayMessage::new_builder().set(content).build();

        net.send(node, SupportProtocols::RelayV3, message.as_bytes());

        let ret = net.should_receive(node, |data: &Bytes| {
            RelayMessage::from_slice(data)
                .map(|message| message.to_enum().item_name() == packed::BlockTransactions::NAME)
                .unwrap_or(false)
        });
        assert!(ret, "Node should response BlockTransactions message");
```

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```
