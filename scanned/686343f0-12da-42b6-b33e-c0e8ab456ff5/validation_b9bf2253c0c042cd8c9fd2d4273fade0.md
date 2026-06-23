### Title
Hardcoded `CorsLayer::permissive()` Exposes Unauthenticated RPC to Cross-Origin Requests from Any Website - (File: rpc/src/server.rs)

### Summary
The CKB RPC server unconditionally applies `CorsLayer::permissive()`, allowing any web origin to issue cross-origin HTTP requests to the node's JSON-RPC endpoint. Because the RPC has no authentication layer and no IP allowlist, a malicious web page visited by a node operator can silently invoke sensitive RPC methods — including `clear_banned_addresses`, `set_network_active`, and `clear_tx_pool` — against the operator's locally-running node. This is a direct analog to the Redpanda finding: a service is reachable by unauthorized parties due to an architectural access-control gap, not merely a user misconfiguration.

### Finding Description

`RpcServer::start_server` in `rpc/src/server.rs` builds the axum router and unconditionally layers `CorsLayer::permissive()` onto every HTTP and WebSocket endpoint:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← hardcoded, no operator override
    ...
```

`CorsLayer::permissive()` responds to every CORS preflight with `Access-Control-Allow-Origin: *` and `Access-Control-Allow-Methods: *`, instructing browsers to permit cross-origin POST requests from any site. The `RpcConfig` struct contains no field for an allowed-origins list, an IP allowlist, or any authentication token:

```rust
pub struct Config {
    pub listen_address: String,
    pub tcp_listen_address: Option<String>,
    pub ws_listen_address: Option<String>,
    pub max_request_body_size: usize,
    pub modules: Vec<Module>,
    // ... no auth, no allowed_origins, no ip_allowlist
}
```

The RPC server accepts and executes every well-formed JSON-RPC call without verifying the caller's identity or origin. The enabled default module set includes `Net`, `Pool`, `Miner`, `Chain`, `Stats`, `Subscription`, and `Experiment` — all served without any credential check.

### Impact Explanation

A malicious web page visited by a node operator can silently POST JSON-RPC calls to `http://127.0.0.1:8114` (the default RPC address). Because `CorsLayer::permissive()` satisfies the browser's CORS preflight, the browser delivers the request and returns the response to the attacker's JavaScript. Exploitable calls include:

- **`clear_banned_addresses`** — erases all IP bans, immediately re-admitting previously-banned malicious P2P peers.
- **`set_network_active(false)`** — shuts down all P2P activity, isolating the node from the network (eclipse/DoS).
- **`clear_tx_pool`** — evicts all pending transactions, disrupting fee-paying users and miners.
- **`set_ban("0.0.0.0/0", "insert", ...)`** — bans every IP, cutting the node off from all peers.
- **`remove_node`** — forcibly disconnects specific peers.

These impacts directly match the Redpanda finding's scope: unauthorized write access and denial-of-service against a service that should be accessible only to trusted local callers.

### Likelihood Explanation

The default `listen_address = "127.0.0.1:8114"` binds only to localhost, so the attack surface is limited to the operator's own browser. However, this is a realistic scenario: node operators routinely use web-based dashboards, block explorers, and dApp frontends in the same browser session as their running node. A single visit to a compromised or malicious site is sufficient. No special privileges, leaked keys, or network access are required — only that the victim's browser can reach `127.0.0.1:8114`, which is always true on the operator's own machine.

### Recommendation

- **Short term**: Replace `CorsLayer::permissive()` with a configurable `CorsLayer` that defaults to rejecting cross-origin requests (e.g., `CorsLayer::new()` with no allowed origins, or an explicit `allowed_origins` config field). Operators who need cross-origin access for legitimate frontends can opt in explicitly.
- **Long term**: Add an optional IP allowlist field to `RpcConfig` so that even network-exposed RPC deployments can restrict callers to known addresses. Consider adding a bearer-token or shared-secret authentication option for the HTTP and TCP RPC endpoints.

### Proof of Concept

With a CKB node running at default settings (`listen_address = "127.0.0.1:8114"`), the following JavaScript executed from any web page in the operator's browser will clear all peer bans:

```javascript
fetch("http://127.0.0.1:8114", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    id: 1,
    jsonrpc: "2.0",
    method: "clear_banned_addresses",
    params: []
  })
}).then(r => r.json()).then(console.log);
// Returns: {"jsonrpc":"2.0","result":null,"id":1}
```

The browser issues a CORS preflight; `CorsLayer::permissive()` responds with `Access-Control-Allow-Origin: *`; the browser delivers the POST; the RPC executes `clear_banned_addresses` with no authentication check; all bans are cleared. The same pattern applies to `set_network_active`, `clear_tx_pool`, and every other enabled RPC method.

**Root cause lines:** [1](#0-0) 

**No authentication or IP filtering in `RpcConfig`:** [2](#0-1) 

**Sensitive methods reachable with no credential check (`clear_banned_addresses`, `set_ban`):** [3](#0-2) 

**TCP RPC server also accepts all connections with no IP check:** [4](#0-3)

### Citations

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

**File:** rpc/src/server.rs (L156-202)
```rust
    async fn start_tcp_server(
        rpc: Arc<MetaIoHandler<Option<Session>>>,
        tcp_listen_address: String,
        handler: Handle,
    ) -> Result<SocketAddr, AnyError> {
        // TCP server with line delimited json codec.
        let listener = TcpListener::bind(tcp_listen_address).await?;
        let tcp_address = listener.local_addr()?;
        handler.spawn(async move {
            let codec = LinesCodec::new_with_max_length(2 * 1024 * 1024);
            let stream_config = StreamServerConfig::default()
                .with_channel_size(4)
                .with_pipeline_size(4)
                .with_shutdown(async move {
                    new_tokio_exit_rx().cancelled().await;
                });

            let exit_signal: CancellationToken = new_tokio_exit_rx();
            tokio::select! {
                _ = async {
                        while let Ok((stream, _)) = listener.accept().await {
                            let rpc = Arc::clone(&rpc);
                            let stream_config = stream_config.clone();
                            let codec = codec.clone();
                            tokio::spawn(async move {
                                let (r, w) = stream.into_split();
                                let r = FramedRead::new(r, codec.clone()).map_ok(StreamMsg::Str);
                                let w = FramedWrite::new(w, codec).with(|msg| async move {
                                    Ok::<_, LinesCodecError>(match msg {
                                        StreamMsg::Str(msg) => msg,
                                        _ => "".into(),
                                    })
                                });
                                tokio::pin!(w);
                                if let Err(err) = serve_stream_sink(&rpc, w, r, stream_config).await {
                                    info!("TCP RPCServer error: {:?}", err);
                                }
                            });
                        }
                    } => {},
                _ = exit_signal.cancelled() => {
                    info!("TCP RPCServer stopped");
                }
            }
        });
        Ok(tcp_address)
    }
```

**File:** util/app-config/src/configs/rpc.rs (L24-61)
```rust
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Eq)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// RPC server listen addresses.
    pub listen_address: String,
    /// RPC TCP server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub tcp_listen_address: Option<String>,
    /// RPC WS server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub ws_listen_address: Option<String>,
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
    /// Number of RPC worker threads.
    pub threads: Option<usize>,
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
    /// Enabled RPC modules.
    pub modules: Vec<Module>,
    /// Rejects txs with scripts that might trigger known bugs
    #[serde(default)]
    pub reject_ill_transactions: bool,
    /// Whether enable deprecated RPC methods.
    ///
    /// Deprecated RPC methods are disabled by default.
    #[serde(default)]
    pub enable_deprecated_rpc: bool,
    /// Customized extra well known lock scripts.
    #[serde(default)]
    pub extra_well_known_lock_scripts: Vec<Script>,
    /// Customized extra well known type scripts.
    #[serde(default)]
    pub extra_well_known_type_scripts: Vec<Script>,
}
```

**File:** rpc/src/module/net.rs (L686-727)
```rust
    fn clear_banned_addresses(&self) -> Result<()> {
        self.network_controller.clear_banned_addrs();
        Ok(())
    }

    fn set_ban(
        &self,
        address: String,
        command: String,
        ban_time: Option<Timestamp>,
        absolute: Option<bool>,
        reason: Option<String>,
    ) -> Result<()> {
        let ip_network = address.parse().map_err(|_| {
            RPCError::invalid_params(format!(
                "Expected `params[0]` to be a valid IP address, got {address}"
            ))
        })?;

        match command.as_ref() {
            "insert" => {
                let ban_until = if absolute.unwrap_or(false) {
                    ban_time.unwrap_or_default().into()
                } else {
                    unix_time_as_millis()
                        + ban_time
                            .unwrap_or_else(|| DEFAULT_BAN_DURATION.into())
                            .value()
                };
                self.network_controller
                    .ban(ip_network, ban_until, reason.unwrap_or_default());
                Ok(())
            }
            "delete" => {
                self.network_controller.unban(&ip_network);
                Ok(())
            }
            _ => Err(RPCError::invalid_params(format!(
                "Expected `params[1]` to be in the list [insert, delete], got {address}"
            ))),
        }
    }
```
