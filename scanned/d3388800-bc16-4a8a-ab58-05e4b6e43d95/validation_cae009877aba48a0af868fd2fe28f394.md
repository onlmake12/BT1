### Title
Unauthenticated Block Template Injection via Miner Notify HTTP Endpoint - (File: `miner/src/client.rs`)

### Summary
The CKB miner's notify-mode HTTP endpoint accepts block templates from **any source** without authentication or origin validation. An attacker who can reach the miner's listen address can inject a crafted `BlockTemplate` — including a modified `cellbase` redirecting mining rewards — and the miner will unknowingly mine and submit that block.

### Finding Description

The miner binary supports a "notify mode" where the CKB node pushes new block templates to the miner via HTTP POST. The miner binds a TCP listener and handles incoming requests in the `handle` free function: [1](#0-0) 

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

There is **no authentication**, **no source-IP check**, and **no chain-state validation** on the incoming template. Any HTTP client that can reach the miner's listen address can POST a `BlockTemplate` JSON body and have it accepted.

The accepted template is forwarded to `update_block_template`: [2](#0-1) 

The `work_id` guard (`if id != work_id || id == 0`) is trivially bypassed: sending `work_id = 0` satisfies `id == 0` when the miner is in its initial state, and sending any `work_id` that differs from the current one satisfies `id != work_id`. Either way the injected template is accepted and forwarded to workers as new work.

The `BlockTemplate` type contains all fields that determine what the miner mines, including `cellbase` (the coinbase transaction that carries the block reward) and `transactions`: [3](#0-2) 

The miner spawns the notify listener when `config.listen` is set: [4](#0-3) 

The documentation explicitly instructs operators to expose this endpoint so the CKB node can reach it, which in multi-machine mining setups means binding to a non-loopback address: [5](#0-4) 

### Impact Explanation

An attacker who can send an HTTP request to the miner's notify port can:

1. **Redirect mining rewards**: Inject a `BlockTemplate` with a `cellbase` transaction whose output lock script points to the attacker's address. The miner solves the PoW puzzle and submits a valid block, but the block reward goes to the attacker.
2. **Inject arbitrary committed transactions**: The `transactions` field of the injected template is used verbatim. The miner has no mechanism to detect that the template did not originate from its configured CKB node.
3. **Waste hashpower**: Inject templates that reference a non-existent or stale `parent_hash`, causing the miner to produce orphan blocks.

The miner has no visibility into the injection — it logs and mines the work exactly as if it came from the legitimate node.

### Likelihood Explanation

- The notify feature is a **supported, documented production feature** intended for use in mining pool and multi-machine setups where the miner and node run on different hosts.
- In such deployments the listen address is necessarily a non-loopback address, making the endpoint reachable from the network.
- Even on a single machine, any local process (e.g., a compromised dependency or co-located service) can reach a loopback-bound listener.
- The attack requires only the ability to send a single HTTP POST — no credentials, no prior state, no cryptographic material.
- The `work_id` bypass (`work_id = 0`) is unconditional at miner startup and trivially achievable at any time by observing or guessing the current work ID.

### Recommendation

**Short term:**
- Add a shared-secret token to the notify endpoint (e.g., a configurable bearer token in the `Authorization` header). The miner should reject any request that does not present the correct token.
- Optionally, restrict accepted connections to the IP address of the configured CKB node RPC URL.

**Long term:**
- Validate the received `BlockTemplate` against the miner's own view of the chain tip (e.g., verify `parent_hash` matches the last known tip hash obtained from the trusted RPC endpoint) before dispatching it to workers.
- Treat all data arriving on the notify endpoint as untrusted and cross-check critical fields (`cellbase`, `parent_hash`, `compact_target`) against values independently fetched from the configured node.

### Proof of Concept

Assuming the miner is configured with `listen = "0.0.0.0:8888"` (or any reachable address):

```bash
# Craft a BlockTemplate JSON with attacker-controlled cellbase
# (work_id=0 bypasses the guard at miner startup)
curl -X POST http://<miner-ip>:8888 \
  -H "Content-Type: application/json" \
  -d '{
    "version": "0x0",
    "compact_target": "0x1e083126",
    "current_time": "0x174c45e17a3",
    "number": "0x401",
    "epoch": "0x7080019000001",
    "parent_hash": "0xa5f5c85987a15de25661e5a214f2c1449cd803f071acc7999820f25246471f40",
    "cycles_limit": "0xd09dc300",
    "bytes_limit": "0x91c08",
    "uncles_count_limit": "0x2",
    "uncles": [],
    "transactions": [],
    "proposals": [],
    "cellbase": {
      "cycles": null,
      "data": { ... outputs pointing to attacker address ... },
      "hash": "0x..."
    },
    "work_id": "0x0",
    "dao": "0xd495a106684401001e47c0ae1d5930009449d26e32380000000721efd0030000"
  }'
```

The miner accepts the template (satisfying `id == 0`), begins mining it, and upon finding a valid nonce calls `submit_block` with the attacker-controlled block. The node verifies the PoW and commits the block; the block reward is paid to the attacker's lock script.

### Citations

**File:** miner/src/client.rs (L204-232)
```rust
    pub fn spawn_background(self) {
        let client = self.clone();
        if let Some(addr) = self.config.listen {
            ckb_logger::info!("listen notify mode : {}", addr);
            ckb_logger::info!(
                r#"
Please note that ckb-miner runs in notify mode. \
You should configure the corresponding information in CKB block assembler, \
for example:

[block_assembler]
...
notify = ["http://{}"]

Otherwise ckb-miner will malfunction and stop submitting valid blocks after a certain period.
"#,
                addr
            );
            self.handle.spawn(async move {
                client.listen_block_template_notify(addr).await;
            });
            self.blocking_fetch_block_template();
        } else {
            ckb_logger::info!("loop poll mode: interval {}ms", self.config.poll_interval);
            self.handle.spawn(async move {
                client.poll_block_template().await;
            });
        }
    }
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

**File:** util/jsonrpc-types/src/block_template.rs (L13-98)
```rust
#[derive(Clone, Default, Serialize, Deserialize, PartialEq, Eq, Hash, Debug, JsonSchema)]
pub struct BlockTemplate {
    /// Block version.
    ///
    /// Miners must use it unchanged in the assembled block.
    pub version: Version,
    /// The compacted difficulty target for the new block.
    ///
    /// Miners must use it unchanged in the assembled block.
    pub compact_target: Uint32,
    /// The timestamp for the new block.
    ///
    /// CKB node guarantees that this timestamp is larger than the median of the previous 37 blocks.
    ///
    /// Miners can increase it to the current time. It is not recommended to decrease it, since it may violate the median block timestamp consensus rule.
    pub current_time: Timestamp,
    /// The block number for the new block.
    ///
    /// Miners must use it unchanged in the assembled block.
    pub number: BlockNumber,
    /// The epoch progress information for the new block.
    ///
    /// Miners must use it unchanged in the assembled block.
    pub epoch: EpochNumberWithFraction,
    /// The parent block hash of the new block.
    ///
    /// Miners must use it unchanged in the assembled block.
    pub parent_hash: H256,
    /// The cycles limit.
    ///
    /// Miners must keep the total cycles below this limit, otherwise, the CKB node will reject the block
    /// submission.
    ///
    /// It is guaranteed that the block does not exceed the limit if miners do not add new
    /// transactions to the block.
    pub cycles_limit: Cycle,
    /// The block serialized size limit.
    ///
    /// Miners must keep the block size below this limit, otherwise, the CKB node will reject the block
    /// submission.
    ///
    /// It is guaranteed that the block does not exceed the limit if miners do not add new
    /// transaction commitments.
    pub bytes_limit: Uint64,
    /// The uncle count limit.
    ///
    /// Miners must keep the uncles count below this limit, otherwise, the CKB node will reject the
    /// block submission.
    pub uncles_count_limit: Uint64,
    /// Provided valid uncle blocks candidates for the new block.
    ///
    /// Miners must include the uncles marked as `required` in the assembled new block.
    pub uncles: Vec<UncleTemplate>,
    /// Provided valid transactions which can be committed in the new block.
    ///
    /// Miners must include the transactions marked as `required` in the assembled new block.
    pub transactions: Vec<TransactionTemplate>,
    /// Provided proposal ids list of transactions for the new block.
    pub proposals: Vec<ProposalShortId>,
    /// Provided cellbase transaction template.
    ///
    /// Miners must use it as the cellbase transaction without changes in the assembled block.
    pub cellbase: CellbaseTemplate,
    /// Work ID. The miner must submit the new assembled and resolved block using the same work ID.
    pub work_id: Uint64,
    /// Reference DAO field.
    ///
    /// This field is only valid when miners use all and only use the provided transactions in the
    /// template. Two fields must be updated when miners want to select transactions:
    ///
    /// * `S_i`, bytes 16 to 23
    /// * `U_i`, bytes 24 to 31
    ///
    /// See RFC [Deposit and Withdraw in Nervos DAO](https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0023-dao-deposit-withdraw/0023-dao-deposit-withdraw.md#calculation).
    pub dao: Byte32,
    /// The extension for the new block.
    ///
    /// This is a field introduced in [CKB RFC 0031]. Since the activation of [CKB RFC 0044], this
    /// field is at least 32 bytes, and at most 96 bytes. The consensus rule of first 32 bytes is
    /// defined in the RFC 0044.
    ///
    /// [CKB RFC 0031]: https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0031-variable-length-header-field/0031-variable-length-header-field.md
    /// [CKB RFC 0044]: https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0044-ckb-light-client/0044-ckb-light-client.md
    #[serde(default)]
    pub extension: Option<JsonBytes>,
}
```
