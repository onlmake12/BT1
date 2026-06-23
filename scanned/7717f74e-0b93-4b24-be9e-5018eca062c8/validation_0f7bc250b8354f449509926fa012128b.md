### Title
HTTP Basic Auth Credentials Transmitted in Plaintext Over Unencrypted Miner RPC Connection — (`miner/src/client.rs`)

---

### Summary

The CKB miner client supports embedding credentials directly in `rpc_url` (e.g., `http://user:pass@host:8114/`). The `parse_authorization()` function extracts these credentials and attaches them as an `Authorization: Basic <base64>` HTTP header on every outgoing RPC call. Because the CKB RPC server natively supports only plain HTTP — with no TLS — and the default `rpc_url` uses the `http://` scheme, these credentials are transmitted in cleartext over the network. Base64 is not encryption; any passive observer on the network path can trivially decode the header and recover the plaintext credentials.

---

### Finding Description

In `miner/src/client.rs`, the function `parse_authorization()` (lines 380–394) splits the URL authority on `@`, takes the `user:pass` prefix, and encodes it as `Basic <base64>`:

```rust
fn parse_authorization(url: &Uri) -> Option<HeaderValue> {
    let a: Vec<&str> = url.authority()?.as_str().split('@').collect();
    if a.len() >= 2 {
        if a[0].is_empty() { return None; }
        let mut encoded = "Basic ".to_string();
        base64::prelude::BASE64_STANDARD.encode_string(a[0], &mut encoded);
        let mut header = HeaderValue::from_str(&encoded).unwrap();
        header.set_sensitive(true);
        Some(header)
    } else { None }
}
``` [1](#0-0) 

This header is then attached to every outgoing `get_block_template` and `submit_block` RPC request inside `Rpc::new()`:

```rust
if let Some(value) = parse_authorization(&url) {
    req = req.header(hyper::header::AUTHORIZATION, value);
}
``` [2](#0-1) 

The CKB RPC server (`rpc/src/server.rs`) binds only to plain HTTP — it uses `axum::serve` over a raw `TcpListener` with no TLS layer: [3](#0-2) 

The official documentation confirms this explicitly:

> "CKB JSON-RPC only supports HTTP now. If you need SSL, please set up a proxy via Nginx or other HTTP servers." [4](#0-3) 

The default `rpc_url` in the shipped miner configuration template is `http://127.0.0.1:8114/` — plain HTTP: [5](#0-4) 

When an operator configures credentials for a remote node (e.g., `http://user:pass@remote-node:8114/`), every polling call to `get_block_template` and every `submit_block` call transmits `Authorization: Basic dXNlcjpwYXNz` in cleartext over the wire.

Note: although `Rpc::new()` constructs an `HttpsConnector` (line 64), this connector supports both HTTP and HTTPS — it does not enforce HTTPS. The scheme used is entirely determined by the operator-supplied `rpc_url`, and no code path warns or rejects a plain `http://` URL when credentials are present. [6](#0-5) 

---

### Impact Explanation

A passive network observer positioned between the miner process and the CKB node (or any authenticating reverse proxy in front of it) can:

1. Capture the HTTP POST traffic to the RPC endpoint.
2. Extract the `Authorization: Basic <base64>` header.
3. Base64-decode it (trivially, e.g., `echo <value> | base64 -d`) to recover the plaintext `user:pass`.
4. Use those credentials to authenticate to the proxy and issue arbitrary RPC calls — including `submit_block` (to submit attacker-controlled blocks), `send_transaction`, or any other enabled module.

The `set_sensitive(true)` call on the header value only suppresses logging of the value within the Rust process; it has no effect on the bytes transmitted over the network. [7](#0-6) 

---

### Likelihood Explanation

**Medium.** In the common single-machine setup (miner and node on the same host, `127.0.0.1`), network interception is not possible. However, in production mining pool deployments — where the miner client connects to a remote CKB node or a remote authenticating proxy over a LAN or WAN — the credential exposure is directly reachable by any attacker with passive access to the network path (e.g., compromised switch, ARP spoofing on LAN, or a malicious ISP/hosting provider). The feature was explicitly added (`#2604: Allow miner http basic authorization`) and is documented, making real-world use of remote credentials a realistic scenario. [8](#0-7) 

---

### Recommendation

1. **Enforce scheme check when credentials are present.** In `parse_authorization()` or `Rpc::new()`, check whether the URL scheme is `http` while credentials are present, and either reject the configuration with an error or emit a prominent warning that credentials will be transmitted in plaintext.
2. **Add TLS support to the RPC server.** The current design delegates TLS to an external proxy, but the miner client has no way to enforce that the proxy connection itself is encrypted. Native TLS support in the RPC server (or at minimum in the miner client's outbound connection) would close this gap.
3. **Document the risk explicitly** in `ckb-miner.toml` and `rpc/README.md`: credentials embedded in `rpc_url` are only safe when the URL uses `https://` or the connection is confined to localhost.

---

### Proof of Concept

1. Configure `ckb-miner.toml`:
   ```toml
   [miner.client]
   rpc_url = "http://miner_user:secret_pass@remote-ckb-node:8114/"
   ```
2. Run `ckb miner`.
3. On the network path, capture traffic:
   ```
   tcpdump -A -i eth0 'tcp port 8114' | grep Authorization
   ```
4. Observe in the captured output:
   ```
   Authorization: Basic bWluZXJfdXNlcjpzZWNyZXRfcGFzcw==
   ```
5. Decode:
   ```
   echo bWluZXJfdXNlcjpzZWNyZXRfcGFzcw== | base64 -d
   # → miner_user:secret_pass
   ```
6. Use the recovered credentials to authenticate to the proxy and call any enabled RPC method against the CKB node. [9](#0-8) [1](#0-0)

### Citations

**File:** miner/src/client.rs (L60-107)
```rust
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

**File:** miner/src/client.rs (L380-394)
```rust
fn parse_authorization(url: &Uri) -> Option<HeaderValue> {
    let a: Vec<&str> = url.authority()?.as_str().split('@').collect();
    if a.len() >= 2 {
        if a[0].is_empty() {
            return None;
        }
        let mut encoded = "Basic ".to_string();
        base64::prelude::BASE64_STANDARD.encode_string(a[0], &mut encoded);
        let mut header = HeaderValue::from_str(&encoded).unwrap();
        header.set_sensitive(true);
        Some(header)
    } else {
        None
    }
}
```

**File:** rpc/src/server.rs (L133-149)
```rust
        handler.spawn(async move {
            let listener = tokio::net::TcpListener::bind(
                &address
                    .to_socket_addrs()
                    .expect("config listen_address parsed")
                    .next()
                    .expect("config listen_address parsed"),
            )
            .await
            .unwrap();
            let server = axum::serve(listener, app.into_make_service());

            let _ = tx_addr.send(server.local_addr().unwrap());
            let graceful = server.with_graceful_shutdown(async move {
                new_tokio_exit_rx().cancelled().await;
            });
            drop(graceful.await);
```

**File:** rpc/README.md (L7-7)
```markdown
CKB JSON-RPC only supports HTTP now. If you need SSL, please set up a proxy via Nginx or other HTTP servers.
```

**File:** resource/ckb-miner.toml (L51-53)
```text
rpc_url = "http://127.0.0.1:8114/" # {{
# _ => rpc_url = "http://127.0.0.1:{rpc_port}/"
# }}
```
