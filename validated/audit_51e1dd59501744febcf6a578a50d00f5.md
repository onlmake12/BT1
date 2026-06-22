### Title
Missing `pending_compact_blocks` Guard in `GetBlockTransactionsProcess::execute` Enables Historical Block Data Exfiltration and Resource Exhaustion — (`sync/src/relayer/get_block_transactions_process.rs`)

---

### Summary

`GetBlockTransactionsProcess::execute` serves `BlockTransactions` responses for **any block present in the persistent chain store**, not just blocks currently undergoing compact-block reconstruction. An unprivileged remote peer can send `GetBlockTransactions` with the hash of any historically committed block and up to 32,767 transaction indexes, forcing the node to perform a full disk read, transaction collection, serialization, and network send — with no guard against the absence of a matching `pending_compact_blocks` entry.

---

### Finding Description

The handler at `sync/src/relayer/get_block_transactions_process.rs` line 60 queries the persistent store unconditionally:

```rust
if let Some(block) = shared.store().get_block(&block_hash) {
``` [1](#0-0) 

The only input guards are a count check against `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) and `max_uncles_num()`: [2](#0-1) 

There is **no check** that `block_hash` exists in `pending_compact_blocks`. Compare this to `BlockTransactionsProcess::execute`, which gates all processing on an `Entry::Occupied` lookup in `pending_compact_blocks`: [3](#0-2) 

The design intent of `GetBlockTransactions` is to serve missing transactions during active compact-block reconstruction. The receiver side enforces this invariant; the sender side does not.

The rate limiter in `Relayer::try_process` is keyed by `(PeerIndex, message_item_id)` at 30 req/s per peer: [4](#0-3) 

With `MAX_RELAY_PEERS = 128`, an attacker controlling 128 connections can sustain 3,840 `GetBlockTransactions` requests/second, each triggering a full block read from disk, collection of up to 32,767 transactions, serialization, and outbound send.

The integration test in `test/src/specs/relay/get_block_transactions_process.rs` confirms this path is reachable and produces a real response for a committed (non-pending) block: [5](#0-4) 

---

### Impact Explanation

Per-peer: 30 req/s × up to 32,767 tx indexes per request → repeated disk reads of full historical blocks + CPU serialization + outbound bandwidth consumption. With 128 peer connections the multiplier is 128×. Targets with large historical blocks (epoch-boundary blocks, high-throughput periods) amplify the per-request cost. The node's relay I/O thread, disk subsystem, and outbound bandwidth can be saturated without any PoW, stake, or privileged access.

---

### Likelihood Explanation

Any peer that can establish a P2P connection can execute this. Block hashes for historical blocks are publicly available from any chain explorer or synced node. No key material, hashpower, or operator access is required. The attack is repeatable and cheap for the attacker (send small fixed-size messages, receive large responses).

---

### Recommendation

Add a guard at the top of `execute` that checks whether `block_hash` is present in `pending_compact_blocks` before querying the store. If the hash is not pending, return `Status::ok()` (silently ignore) or a non-banning status, consistent with how `BlockTransactionsProcess` handles unknown hashes. Example:

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

---

### Proof of Concept

1. Connect to a non-IBD CKB node as a relay peer.
2. Obtain the hash of any committed block (e.g., genesis block hash, publicly known).
3. Send a `RelayMessage` containing `GetBlockTransactions { block_hash: <genesis_hash>, indexes: [0..N-1], uncle_indexes: [] }` where N ≤ 32,767.
4. Observe the node responds with a `BlockTransactions` message containing the full transaction data.
5. Repeat at 30 req/s (rate limit per peer); open additional connections to multiply throughput.
6. Assert: no `pending_compact_blocks` entry for the genesis hash exists at any point, yet the node serves the response — confirming the missing guard.

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

**File:** test/src/specs/relay/get_block_transactions_process.rs (L36-49)
```rust
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
