### Title
Unauthenticated `log` Topic Subscription Exposes All Internal Node Log Messages to Any RPC Caller - (`rpc/src/module/subscription.rs`)

### Summary

The CKB RPC subscription system exposes a `log` topic that streams all internal node log messages (including debug-level output from sync, relay, tx-pool, and network components) to any caller who invokes `subscribe("log")`. There is no authentication or authorization check on the subscription endpoint. Combined with a `CorsLayer::permissive()` CORS policy on the HTTP/WebSocket server, any web page visited by a node operator can silently subscribe to the full internal log stream via a cross-origin WebSocket connection.

### Finding Description

The `subscribe` method in `SubscriptionRpcImpl` accepts a `Topic` enum and returns a live stream of messages for that topic to the caller with no identity or permission check:

```rust
fn subscribe(&self, topic: Topic) -> Result<Self::S> {
    let tx = match topic {
        // ...
        Topic::Log => self.log_sender.clone(),
    };
    let mut rx = tx.subscribe();
    Ok(Box::pin(async_stream::stream! { /* yields all log messages */ }))
}
``` [1](#0-0) 

The log data originates in `Logger::log()`, which captures every log record matching the main filter and calls `notifier.notify_log()` with the raw `original_message` (i.e., `format!("{}", record.args())`): [2](#0-1) 

`NotifyService::handle_notify_log()` then fans out the `LogEntry` to **all registered log subscribers** with no per-subscriber filtering: [3](#0-2) 

`SubscriptionRpcImpl::new()` registers itself as a log subscriber and re-broadcasts every entry over a `broadcast::Sender<PublishMsg<String>>` that any `subscribe("log")` caller taps into: [4](#0-3) 

The HTTP and WebSocket RPC server is started with `CorsLayer::permissive()`, which allows cross-origin requests from any web origin: [5](#0-4) 

The `Subscription` module (which includes `Topic::Log`) is enabled by default in the standard node configuration: [6](#0-5) 

The default log filter captures debug-level output from `ckb-sync`, `ckb-relay`, `ckb-tx-pool`, and `ckb-network`: [7](#0-6) 

### Impact Explanation

Any process or web page that can reach the RPC endpoint receives a continuous stream of all internal node log messages. These messages include: peer IP addresses and connection events, transaction processing decisions, block relay and sync state, error messages with internal paths and state, and debug-level operational details from multiple subsystems. This information can be used to fingerprint the node's peer topology, monitor mempool activity, or identify operational patterns useful for targeted attacks. The `CorsLayer::permissive()` policy means a malicious web page visited by the node operator can silently open a WebSocket to `ws://localhost:28114` and subscribe to the full log stream without any user interaction beyond page load.

**Impact: 3/5**

### Likelihood Explanation

The `Subscription` module is enabled by default. The WebSocket endpoint is opt-in (`ws_listen_address`), but the HTTP endpoint with permissive CORS is always active on `127.0.0.1:8114`. Any local process or browser tab can reach it. If the operator has enabled `tcp_listen_address` or `ws_listen_address` on a non-loopback interface, the attack surface extends to the network. No credentials, keys, or privileged access are required — a single JSON-RPC call is sufficient.

**Likelihood: 3/5**

### Recommendation

1. Require explicit opt-in or a separate access-control flag to enable the `log` subscription topic, distinct from other topics.
2. Replace `CorsLayer::permissive()` with a restrictive CORS policy (e.g., same-origin only or an operator-configured allowlist) on the HTTP/WebSocket RPC server.
3. Consider adding an authentication layer (e.g., token-based) to the TCP and WebSocket subscription endpoints when they are exposed beyond localhost.

### Proof of Concept

With a CKB node running with default config and WebSocket enabled (`ws_listen_address = "127.0.0.1:28114"`):

```javascript
// From any web page (CORS is permissive, so cross-origin works)
let ws = new WebSocket("ws://127.0.0.1:28114");
ws.onopen = () => {
    ws.send(JSON.stringify({
        id: 1, jsonrpc: "2.0",
        method: "subscribe",
        params: ["log"]
    }));
};
ws.onmessage = (event) => {
    // Receives ALL internal node log messages in real time
    console.log(event.data);
};
```

Or via TCP with no authentication:

```bash
telnet 127.0.0.1 18114
{"id":1,"jsonrpc":"2.0","method":"subscribe","params":["log"]}
# Node streams all log entries matching the configured filter level
```

The `subscribe` function performs no caller identity check before granting access to `Topic::Log`, and `handle_notify_log` dispatches to all subscribers unconditionally — a direct analog to `window.postMessage(message, '*')` broadcasting to any origin. [8](#0-7) [3](#0-2) [9](#0-8)

### Citations

**File:** rpc/src/module/subscription.rs (L214-239)
```rust
    fn subscribe(&self, topic: Topic) -> Result<Self::S> {
        let tx = match topic {
            Topic::NewTipHeader => self.new_tip_header_sender.clone(),
            Topic::NewTipBlock => self.new_tip_block_sender.clone(),
            Topic::NewTransaction => self.new_transaction_sender.clone(),
            Topic::ProposedTransaction => self.proposed_transaction_sender.clone(),
            Topic::RejectedTransaction => self.new_reject_transaction_sender.clone(),
            Topic::Log => self.log_sender.clone(),
        };
        let mut rx = tx.subscribe();
        Ok(Box::pin(async_stream::stream! {
                loop {
                    match rx.recv().await {
                        Ok(msg) => {
                            yield msg;
                        }
                        Err(RecvError::Lagged(cnt)) => {
                            error!("subscription lagged error: {:?}", cnt);
                        }
                        Err(RecvError::Closed) => {
                            break;
                        }
                    }
                }
        }))
    }
```

**File:** rpc/src/module/subscription.rs (L272-309)
```rust
        let mut log_receiver =
            handle.block_on(notify_controller.subscribe_log(SUBSCRIBER_NAME.to_string()));

        let (new_tip_header_sender, _) = broadcast::channel(NOTIFY_CHANNEL_SIZE);
        let (new_tip_block_sender, _) = broadcast::channel(NOTIFY_CHANNEL_SIZE);
        let (proposed_transaction_sender, _) = broadcast::channel(NOTIFY_CHANNEL_SIZE);
        let (new_transaction_sender, _) = broadcast::channel(NOTIFY_CHANNEL_SIZE);
        let (new_reject_transaction_sender, _) = broadcast::channel(NOTIFY_CHANNEL_SIZE);
        let (log_sender, _) = broadcast::channel(NOTIFY_CHANNEL_SIZE);

        let stop_rx = new_tokio_exit_rx();
        handle.spawn({
            let new_tip_header_sender = new_tip_header_sender.clone();
            let new_tip_block_sender = new_tip_block_sender.clone();
            let new_transaction_sender = new_transaction_sender.clone();
            let proposed_transaction_sender = proposed_transaction_sender.clone();
            let new_reject_transaction_sender = new_reject_transaction_sender.clone();
            let log_sender = log_sender.clone();
            async move {
                loop {
                    tokio::select! {
                        Some(block) = new_block_receiver.recv() => {
                            publiser_send!(ckb_jsonrpc_types::HeaderView, block.header(), new_tip_header_sender);
                            publiser_send!(ckb_jsonrpc_types::BlockView, block, new_tip_block_sender);
                        },
                        Some(tx_entry) = new_transaction_receiver.recv() => {
                            publiser_send!(ckb_jsonrpc_types::PoolTransactionEntry, tx_entry, new_transaction_sender);
                        },
                        Some(tx_entry) = proposed_transaction_receiver.recv() => {
                            publiser_send!(ckb_jsonrpc_types::PoolTransactionEntry, tx_entry, proposed_transaction_sender);
                        },
                        Some((tx_entry, reject)) = reject_transaction_receiver.recv() => {
                            publiser_send!((ckb_jsonrpc_types::PoolTransactionEntry, ckb_jsonrpc_types::PoolTransactionReject),
                                            (tx_entry.into(), reject.into()),
                                            new_reject_transaction_sender);
                        },
                        Some(log_entry) = log_receiver.recv() => {
                            publiser_send!(ckb_jsonrpc_types::LogEntry, convert_log_entry(log_entry), log_sender);
```

**File:** util/logger-service/src/lib.rs (L218-226)
```rust
                            if is_match {
                                if let Some(notifier) = &notifier {
                                    notifier.notify_log(LogEntry {
                                        level,
                                        message: original_message,
                                        date,
                                        target,
                                    });
                                }
```

**File:** notify/src/lib.rs (L455-462)
```rust
    fn handle_notify_log(&self, log_entry: LogEntry) {
        for subscriber in self.log_subscribers.values() {
            let log_entry = log_entry.clone();
            let subscriber = subscriber.clone();
            // Ignore failures
            subscriber.try_send(log_entry).ok();
        }
    }
```

**File:** rpc/src/server.rs (L119-129)
```rust
        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** test/template/ckb.toml (L13-13)
```text
filter = "info,ckb-rpc=debug,ckb-sync=debug,ckb-relay=debug,ckb-tx-pool=debug,ckb-network=debug"
```
