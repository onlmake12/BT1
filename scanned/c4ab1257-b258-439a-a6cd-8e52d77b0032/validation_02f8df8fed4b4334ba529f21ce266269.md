### Title
Missing Timeout on Miner HTTP RPC Client Causes Indefinite Blocking — (File: `miner/src/client.rs`)

### Summary
The `Rpc` HTTP client used by the CKB miner to communicate with the CKB node's JSON-RPC endpoint is constructed without any request timeout. If the CKB node accepts the TCP connection but never sends a response, the miner's `submit_block` and `get_block_template` calls will hang indefinitely, stalling the miner process permanently.

### Finding Description

In `miner/src/client.rs`, the `Rpc::new` function constructs a `hyper_util` HTTP client with no timeout:

```rust
let https = hyper_tls::HttpsConnector::new();
let client = HttpClient::builder(TokioExecutor::new()).build::<_, Full<Bytes>>(https);
``` [1](#0-0) 

No `.pool_idle_timeout`, `.pool_max_idle_per_host`, or any request-level timeout is applied. The `hyper_util` legacy client has no built-in default request timeout.

Every RPC call — `get_block_template` and `submit_block` — is dispatched through this client:

```rust
let request = match client.request(req).await ...
``` [2](#0-1) 

The `ClientConfig` struct has no timeout field whatsoever: [3](#0-2) 

The most severe path is `submit_block` with `block_on_submit = true`, which synchronously blocks the calling thread:

```rust
if self.config.block_on_submit {
    self.handle.block_on(future).map(|_| ())
``` [4](#0-3) 

`handle.block_on(future)` parks the calling OS thread until the future resolves. If the CKB node's RPC server accepts the TCP connection but never sends a response body, this call never returns, permanently stalling the miner's block submission pipeline.

Even in the non-blocking path (`block_on_submit = false`), spawned tasks accumulate in the Tokio runtime without bound, each holding an open HTTP connection and awaiting a response that never arrives, consuming file descriptors and memory.

### Impact Explanation

The miner process hangs indefinitely and cannot submit solved blocks or fetch new block templates. This is a direct liveness failure: valid PoW solutions are silently discarded, and the miner contributes no blocks to the network for the duration of the hang. With `block_on_submit = true` (the default in `resource/ckb-miner.toml`), the hang is synchronous and total — the miner thread is parked and no recovery is possible without a process restart. [5](#0-4) 

### Likelihood Explanation

The CKB node's RPC server is typically local (`127.0.0.1:8114`), but the `rpc_url` is operator-configurable and can point to any remote host. A CKB node under heavy load, a slow `get_block_template` computation (e.g., large mempool), or any network-level TCP accept-but-no-response condition (e.g., a firewall that accepts SYN but drops data packets) triggers this. This is a realistic operational scenario, not a theoretical one.

### Recommendation

Apply a request-level timeout to the `hyper_util` HTTP client in `Rpc::new`. The simplest approach is to wrap each `client.request(req)` call with `tokio::time::timeout(duration, ...)`. A configurable `rpc_timeout_secs` field should be added to `ClientConfig` in `util/app-config/src/configs/miner.rs` and threaded through to `Rpc::new`. [6](#0-5) 

### Proof of Concept

1. Configure `ckb-miner.toml` with `block_on_submit = true` (the default).
2. Start a TCP listener on the miner's configured `rpc_url` port that accepts connections but never sends any data (e.g., `nc -l 8114`).
3. Start `ckb-miner`. It will call `get_block_template`, which dispatches through `Rpc::request` → `client.request(req).await`. The `await` never resolves.
4. With `block_on_submit = true`, the first `submit_block` call invokes `handle.block_on(future)`, which parks the thread permanently.
5. The miner process is now fully stalled: no new templates are fetched, no blocks are submitted, and the process must be killed and restarted to recover. [2](#0-1) [7](#0-6)

### Citations

**File:** miner/src/client.rs (L59-107)
```rust
impl Rpc {
    pub fn new(url: Uri, handle: Handle) -> Rpc {
        let (sender, mut receiver) = mpsc::channel(65_535);
        let stop_rx: CancellationToken = new_tokio_exit_rx();

        let https = hyper_tls::HttpsConnector::new();
        let client = HttpClient::builder(TokioExecutor::new()).build::<_, Full<Bytes>>(https);
        let loop_handle = handle.clone();
        handle.spawn(async move {
            loop {
                tokio::select! {
                    Some(item) = receiver.recv() => {
                        let (sender, call): RpcRequest = item;
                        let req_url = url.clone();
                        let request_json = serde_json::to_vec(&call).expect("valid rpc call");

                        let mut req = Request::builder().uri(req_url).method("POST").header(CONTENT_TYPE, "application/json");

                        if let Some(value) = parse_authorization(&url) {
                            req = req
                                .header(hyper::header::AUTHORIZATION, value);
                        }
                        let req = req.body(Full::new(Bytes::from(request_json))).unwrap();
                        let client = client.clone();
                        loop_handle.spawn(async move {
                            let request = match client
                                .request(req)
                                .await
                                .map(|res|res.into_body())
                            {
                                Ok(body) => BodyExt::collect(body).await.map_err(RpcError::Http).map(|t| t.to_bytes()),
                                Err(err) => Err(RpcError::Client(err)),
                            };
                            if sender.send(request).is_err() {
                                error!("rpc response send back error")
                            }
                        });
                    },
                    _ = stop_rx.cancelled() => {
                        info!("Rpc server received exit signal, exit now");
                        break
                    },
                    else => break
                }
            }
        });

        Rpc { sender }
    }
```

**File:** miner/src/client.rs (L183-201)
```rust
    pub(crate) fn submit_block(&self, work_id: &str, block: Block) -> Result<(), RpcError> {
        let parent = block.header().raw().parent_hash();
        let future = self
            .send_submit_block_request(work_id, block)
            .and_then(parse_response::<H256>);

        if self.config.block_on_submit {
            self.handle.block_on(future).map(|_| ())
        } else {
            let sender = self.new_work_tx.clone();
            self.handle.spawn(async move {
                if let Err(e) = future.await {
                    error!("rpc call submit_block error: {:?}", e);
                    sender.send(Works::FailSubmit(parent)).unwrap()
                }
            });
            Ok(())
        }
    }
```

**File:** util/app-config/src/configs/miner.rs (L17-30)
```rust
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Eq)]
#[serde(deny_unknown_fields)]
pub struct ClientConfig {
    /// CKB node RPC endpoint.
    pub rpc_url: String,
    /// The poll interval in seconds to get work from the CKB node.
    pub poll_interval: u64,
    /// By default, miner submits a block and continues to get the next work.
    ///
    /// When this is enabled, miner will block until the submission RPC returns.
    pub block_on_submit: bool,
    /// listen block_template notify instead of loop poll
    pub listen: Option<SocketAddr>,
}
```

**File:** resource/ckb-miner.toml (L50-57)
```text
[miner.client]
rpc_url = "http://127.0.0.1:8114/" # {{
# _ => rpc_url = "http://127.0.0.1:{rpc_port}/"
# }}
block_on_submit = true

# block template polling interval in milliseconds
poll_interval = 1000
```
