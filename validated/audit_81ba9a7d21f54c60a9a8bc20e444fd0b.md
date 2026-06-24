Audit Report

## Title
No Rate Limiting on `get_transaction` RPC Handler Saturates Shared Tx-Pool Actor Queue - (File: rpc/src/module/chain.rs)

## Summary
The `get_transaction` RPC endpoint has no rate limiting at any layer. With the default `only_committed=false`, requests for unknown transaction hashes unconditionally enqueue `GetTransactionWithStatus` messages to the single-threaded tx-pool service actor, which processes all messages sequentially from a bounded 512-slot channel. An unprivileged attacker flooding this endpoint can saturate the queue, delaying or blocking `BlockTemplate`, `SubmitLocalTx`, and `SubmitRemoteTx` operations that share the same channel.

## Finding Description
`rpc/src/server.rs` (L119-129) builds an Axum router with only a `CorsLayer` and a 30-second `TimeoutLayer`. No rate limiting, connection concurrency limit, or semaphore is present — confirmed by zero matches for `rate_limit`, `RateLimiter`, `throttle`, `max_connections`, `connection_limit`, `semaphore`, or `concurrency_limit` across all `rpc/**/*.rs` files.

In `rpc/src/module/chain.rs` (L1760), `only_committed` defaults to `false`. When a transaction hash is not found in the committed chain snapshot (L2191), the early return is skipped. At L2214-2219, `only_committed=false` causes the code to call `tx_pool.get_transaction_with_status(tx_hash)`, which enqueues a `Message::GetTransactionWithStatus` to the tx-pool actor.

The tx-pool service actor (`tx-pool/src/service.rs`) uses a bounded channel of size `DEFAULT_CHANNEL_SIZE = 512` (L53). All message variants — `BlockTemplate`, `SubmitLocalTx`, `SubmitRemoteTx`, `GetTransactionWithStatus`, etc. — share this single channel (L117-149). The actor processes messages sequentially in a single loop.

Handling `GetTransactionWithStatus` (L904-942) acquires `service.tx_pool.read().await` (a shared async read lock on the entire pool map) and then queries the `recent_reject` RocksDB database. For random hashes that are never in the pool or recent-reject DB, this path always reaches the RocksDB lookup before returning `TransactionWithStatus::with_unknown()`.

An attacker flooding `get_transaction` with random hashes fills the 512-slot channel with `GetTransactionWithStatus` messages. Each message holds the read lock during a RocksDB lookup. `BlockTemplate` and `SubmitLocalTx` messages queued behind them are delayed until all flood messages drain. The 30-second `TimeoutLayer` means each attacker request can occupy a queue slot for up to 30 seconds, and with no connection limit, the attacker can sustain saturation continuously.

## Impact Explanation
Targeting a mining node's RPC endpoint allows an attacker to block `BlockTemplate` responses, preventing miners from obtaining fresh block templates. Mining pools commonly expose or proxy the RPC endpoint for operational reasons. Sustained queue saturation across targeted mining infrastructure degrades block production rates on the CKB network. This matches: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. The attacker cost is negligible unauthenticated HTTP POST requests with random 32-byte hashes.

## Likelihood Explanation
The RPC endpoint requires no authentication. The default bind is `127.0.0.1:8114`, but mining pool operators routinely expose it publicly or via reverse proxies. The attacker needs only to send concurrent JSON-RPC POST requests with arbitrary 32-byte hashes. Random hashes will never match committed transactions, ensuring every request reaches the tx-pool actor path. The attack is trivially scriptable, requires no special knowledge, and is indefinitely repeatable.

## Recommendation
1. Add per-IP or global rate limiting middleware (e.g., `tower_governor` or a custom Tower layer) to the Axum router in `rpc/src/server.rs` before the `handle_jsonrpc` handler.
2. Add a connection concurrency limit (e.g., `tower::limit::ConcurrencyLimitLayer`) to bound simultaneous in-flight RPC requests.
3. Use a separate bounded channel or priority queue for read-only tx-pool queries (`GetTransactionWithStatus`, `GetTxStatus`) vs. write/critical operations (`SubmitLocalTx`, `BlockTemplate`), so query flooding cannot starve block template generation.
4. Document and encourage `only_committed=true` for read-heavy use cases, which bypasses the tx-pool actor entirely.

## Proof of Concept
```bash
# Flood get_transaction with random hashes (no authentication needed)
for i in $(seq 1 10000); do
  curl -s -X POST http://<node>:8114/ \
    -H 'Content-Type: application/json' \
    -d '{"id":1,"jsonrpc":"2.0","method":"get_transaction","params":["0x'$(openssl rand -hex 32)'"]}' &
done

# Simultaneously attempt block template generation (miner path):
curl -X POST http://<node>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"get_block_template","params":[null,null,null]}'
# Expected: get_block_template is delayed or times out due to tx-pool queue saturation.
```

Each flood request with a random hash bypasses the committed-chain lookup at `chain.rs` L2191, hits the `only_committed=false` path at L2214, and enqueues a `GetTransactionWithStatus` message that acquires `tx_pool.read().await` and queries RocksDB at `service.rs` L909-934, with no throttle at any layer. The bounded channel of 512 slots (`service.rs` L53) is saturated, and `BlockTemplate` messages queued behind them are delayed until all flood messages drain.