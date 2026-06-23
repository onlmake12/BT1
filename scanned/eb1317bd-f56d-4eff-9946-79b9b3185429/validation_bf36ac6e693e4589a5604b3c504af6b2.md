### Title
CKB Miner Accepts Unencrypted `http://` RPC URL for External Hosts Without Warning - (File: `miner/src/client.rs`)

---

### Summary

The CKB built-in miner (`ckb miner`) accepts an arbitrary `rpc_url` in `ckb-miner.toml` and connects to it over plaintext HTTP with no validation of the URL scheme or host locality. When a miner operator points `rpc_url` at an external host using `http://`, no warning or rejection is issued. An attacker on the same network can perform a man-in-the-middle attack, intercepting and modifying block templates and block submissions between the miner and the CKB node.

---

### Finding Description

The CKB miner is explicitly designed to run as a separate process from the CKB node, communicating with it via JSON-RPC. The `[miner.client]` section of `ckb-miner.toml` exposes an `rpc_url` field:

```toml
[miner.client]
rpc_url = "http://127.0.0.1:8114/"
```

The `ClientConfig` struct stores this as a plain `String` with no validation: [1](#0-0) 

In `Client::new`, the URL is parsed only for syntactic validity using `Uri::parse`, with no check on the scheme (`http` vs `https`) or whether the host is a loopback address: [2](#0-1) 

The resulting `Uri` is passed directly to `Rpc::new`, which immediately begins making plaintext HTTP POST requests to it: [3](#0-2) 

The CKB RPC server itself only supports HTTP (no TLS), as documented explicitly: [4](#0-3) 

This means that when a miner operator configures `rpc_url = "http://<external-node-ip>:8114/"` — a supported and realistic deployment where the miner and node run on separate machines — all traffic between the miner and node is unencrypted and unauthenticated, with no warning issued at startup.

By contrast, the proxy URL validation in `network/src/proxy.rs` demonstrates that CKB does perform scheme and host validation in other URL-consuming paths: [5](#0-4) 

No equivalent validation exists for `rpc_url`.

---

### Impact Explanation

An attacker on the same network as the miner (or performing ARP spoofing) can intercept and modify:

1. **`get_block_template` responses** — The attacker can replace the `coinbase` output's lock script with their own address, redirecting all mining rewards to themselves. They can also inject or remove transactions from the template.
2. **`submit_block` requests** — The attacker can silently drop block submissions, causing the miner to lose valid block rewards and waste hashpower.
3. **Stale template injection** — The attacker can replay old block templates, causing the miner to work on already-solved or orphaned blocks.

The miner's `submit_block` call carries the fully assembled block including the coinbase transaction. Tampering with the coinbase address is a direct financial loss to the miner operator.

---

### Likelihood Explanation

The CKB miner is explicitly designed to run as a separate process from the node. The CHANGELOG confirms: *"The miner now runs as a separate process."* In production mining setups, it is common to run the miner on dedicated hardware separate from the node, requiring a non-loopback `rpc_url`. The default template uses `http://127.0.0.1:8114/`, but operators who deploy across machines will naturally change this to an external IP. No documentation warns against using `http://` for external hosts, and no runtime check prevents it. The attack requires only network adjacency (LAN, data center network, or cloud VPC), which is realistic for any miner running on a separate machine from its CKB node.

---

### Recommendation

At startup in `Client::new`, after parsing `config.rpc_url` into a `Uri`, check whether the scheme is `http` and the host is non-loopback. If so, emit a prominent warning log message informing the operator that the connection is unencrypted and susceptible to man-in-the-middle attacks. Optionally, reject the configuration unless an explicit `--allow-insecure-rpc` flag is passed.

Example check location: [6](#0-5) 

---

### Proof of Concept

1. Configure `ckb-miner.toml` with an external node IP:
   ```toml
   [miner.client]
   rpc_url = "http://192.168.1.100:8114/"
   ```
2. Start `ckb miner`. No warning is emitted; the miner silently connects over plaintext HTTP.
3. An attacker on the same LAN performs ARP spoofing to position themselves between the miner and the node.
4. The attacker intercepts the `get_block_template` JSON-RPC response and replaces the `block_template.cellbase.outputs[0].lock` with their own lock script (their address).
5. The miner assembles and submits a block with the attacker's coinbase address. If the block is accepted by the network, the block reward is paid to the attacker.
6. The miner operator receives no warning and has no indication the attack occurred. [7](#0-6) [8](#0-7)

### Citations

**File:** util/app-config/src/configs/miner.rs (L19-21)
```rust
pub struct ClientConfig {
    /// CKB node RPC endpoint.
    pub rpc_url: String,
```

**File:** miner/src/client.rs (L60-65)
```rust
    pub fn new(url: Uri, handle: Handle) -> Rpc {
        let (sender, mut receiver) = mpsc::channel(65_535);
        let stop_rx: CancellationToken = new_tokio_exit_rx();

        let https = hyper_tls::HttpsConnector::new();
        let client = HttpClient::builder(TokioExecutor::new()).build::<_, Full<Bytes>>(https);
```

**File:** miner/src/client.rs (L73-95)
```rust
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
```

**File:** miner/src/client.rs (L159-168)
```rust
    pub fn new(new_work_tx: Sender<Works>, config: MinerClientConfig, handle: Handle) -> Client {
        let uri: Uri = config.rpc_url.parse().expect("valid rpc url");

        Client {
            current_work_id: Arc::new(AtomicU64::new(0)),
            rpc: Rpc::new(uri, handle.clone()),
            new_work_tx,
            config,
            handle,
        }
```

**File:** rpc/README.md (L7-7)
```markdown
CKB JSON-RPC only supports HTTP now. If you need SSL, please set up a proxy via Nginx or other HTTP servers.
```

**File:** network/src/proxy.rs (L3-15)
```rust
pub(crate) fn check_proxy_url(proxy_url: &str) -> Result<(), String> {
    let parsed_url = Url::parse(proxy_url).map_err(|e| e.to_string())?;
    if parsed_url.host_str().is_none() {
        return Err(format!("missing host in proxy url: {}", proxy_url));
    }
    let scheme = parsed_url.scheme();
    if scheme.ne("socks5") {
        return Err(format!("CKB doesn't support proxy scheme: {}", scheme));
    }
    if parsed_url.port().is_none() {
        return Err(format!("missing port in proxy url: {}", proxy_url));
    }
    Ok(())
```

**File:** resource/ckb-miner.toml (L50-53)
```text
[miner.client]
rpc_url = "http://127.0.0.1:8114/" # {{
# _ => rpc_url = "http://127.0.0.1:{rpc_port}/"
# }}
```
