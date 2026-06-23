### Title
LightClientProtocol Serves Cryptographic State Proofs During IBD Without Guard — (`util/light-client-protocol-server/src/lib.rs`)

---

### Summary

The `LightClientProtocol::received` handler processes all incoming light-client messages (including `GetLastState`, `GetLastStateProof`, `GetBlocksProof`, `GetTransactionsProof`) without first checking whether the node is in Initial Block Download (IBD) mode. The Relayer protocol handler explicitly guards against this with an early-return IBD check, but the analogous guard is absent from `LightClientProtocol`. During IBD the node's chain state is incomplete and its MMR chain-root is still being built; serving cryptographic proofs from this state can mislead connected light clients into accepting an incorrect or unfinished chain tip as authoritative.

---

### Finding Description

**Vulnerable file:** `util/light-client-protocol-server/src/lib.rs`, lines 55–92

The `LightClientProtocol::received` function parses the incoming message and immediately dispatches it to `try_process` with no IBD guard:

```rust
async fn received(
    &mut self,
    nc: Arc<dyn CKBProtocolContext + Sync>,
    peer: PeerIndex,
    data: Bytes,
) {
    trace!("LightClient.received peer={}", peer);
    let msg = match packed::LightClientMessageReader::from_slice(&data) { ... };
    let item_name = msg.item_name();
    let status = self.try_process(&nc, peer, msg).await;   // ← no IBD check
    ...
}
```

Compare this with the Relayer, which explicitly guards at the top of its `received` handler:

```rust
// sync/src/relayer/mod.rs  line 815-818
// If self is in the IBD state, don't process any relayer message.
if self.shared.active_chain().is_initial_block_download() {
    return;
}
```

`try_process` dispatches to four component handlers:

| Message | Handler |
|---|---|
| `GetLastState` | `GetLastStateProcess` |
| `GetLastStateProof` | `GetLastStateProofProcess` |
| `GetBlocksProof` | `GetBlocksProofProcess` |
| `GetTransactionsProof` | `GetTransactionsProofProcess` |

`GetLastStateProcess::execute` calls `self.protocol.get_verifiable_tip_header()`, which reads the current snapshot tip and builds an MMR root from `snapshot.chain_root_mmr(tip_block.number() - 1)`. During IBD the MMR is still being populated and the tip may be an unverified block (especially when `assume_valid_target` is active). The resulting `VerifiableHeader` and any derived proofs are therefore based on an incomplete, potentially unverified chain state.

The `is_initial_block_download` check in `Shared` is well-defined and available to the light-client handler via `self.shared.is_initial_block_download()`.

---

### Impact Explanation

A light client that connects to a full node in IBD and sends `GetLastState` or `GetLastStateProof` receives a `VerifiableHeader` whose embedded MMR root reflects only the blocks processed so far, not the eventual canonical chain. The light client may:

1. Accept this incomplete tip as the authoritative last state and anchor all subsequent proof queries to it.
2. Receive `GetBlocksProof` or `GetTransactionsProof` responses that are cryptographically self-consistent with the incomplete MMR but do not reflect the final verified chain.
3. Conclude that a transaction is confirmed when the block containing it has not yet been validated by the full node.

Because the light-client protocol is designed to let resource-constrained clients trust the full node's proofs, the absence of the IBD guard breaks the trust model: the full node is implicitly asserting "this is my verified chain state" when it is not.

---

### Likelihood Explanation

Any unprivileged peer that negotiates the `LightClient` sub-protocol can trigger this by sending a `GetLastState` message immediately after connecting, before the full node exits IBD. A newly started node remains in IBD for an extended period (hours to days on mainnet). The entry path requires no special privileges: connect, negotiate `SupportProtocols::LightClient`, send one message.

---

### Recommendation

Add an IBD guard at the top of `LightClientProtocol::received`, mirroring the Relayer pattern:

```rust
async fn received(
    &mut self,
    nc: Arc<dyn CKBProtocolContext + Sync>,
    peer: PeerIndex,
    data: Bytes,
) {
+   if self.shared.is_initial_block_download() {
+       return;
+   }
    trace!("LightClient.received peer={}", peer);
    ...
}
```

Alternatively, return a dedicated `StatusCode` (e.g., `NodeInIBD`) so the light client can retry later, rather than silently dropping the message.

---

### Proof of Concept

1. Start a CKB full node from genesis (IBD state confirmed: `is_initial_block_download = true`).
2. Connect a peer that negotiates `SupportProtocols::LightClient`.
3. Send a `GetLastState { subscribe: false }` message.
4. The node responds with `SendLastState` containing a `VerifiableHeader` whose `parent_chain_root` is derived from an incomplete MMR (`snapshot.chain_root_mmr(tip_block.number() - 1)` at `util/light-client-protocol-server/src/lib.rs` lines 137–144).
5. The Relayer handler at `sync/src/relayer/mod.rs:816` would have returned immediately for the same node state — demonstrating the inconsistent guard policy.

**Root cause location:** [1](#0-0) 

**Missing guard (compare with Relayer):** [2](#0-1) 

**IBD check implementation:** [3](#0-2) 

**Incomplete MMR root served during IBD:** [4](#0-3) 

**Documented intent ("stops responding to most P2P requests" during IBD):** [5](#0-4)

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L55-92)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        data: Bytes,
    ) {
        trace!("LightClient.received peer={}", peer);

        let msg = match packed::LightClientMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "LightClient.received a malformed message from Peer({})",
                    peer
                );
                nc.ban_peer(
                    peer,
                    constant::BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();
        let status = self.try_process(&nc, peer, msg).await;
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
        } else if status.should_warn() {
            warn!("process {} from {}; result is {}", item_name, peer, status);
        } else if !status.is_ok() {
            debug!("process {} from {}; result is {}", item_name, peer, status);
        }
    }
```

**File:** util/light-client-protocol-server/src/lib.rs (L127-154)
```rust
    pub(crate) fn get_verifiable_tip_header(&self) -> Result<packed::VerifiableHeader, String> {
        let snapshot = self.shared.snapshot();

        let tip_hash = snapshot.tip_hash();
        let tip_block = snapshot
            .get_block(&tip_hash)
            .expect("checked: tip block should be existed");
        let parent_chain_root = if tip_block.is_genesis() {
            Default::default()
        } else {
            let mmr = snapshot.chain_root_mmr(tip_block.number() - 1);
            match mmr.get_root() {
                Ok(root) => root,
                Err(err) => {
                    let errmsg = format!("failed to generate a root since {err:?}");
                    return Err(errmsg);
                }
            }
        };

        let tip_header = packed::VerifiableHeader::new_builder()
            .header(tip_block.header().data())
            .uncles_hash(tip_block.calc_uncles_hash())
            .extension(Pack::pack(&tip_block.extension()))
            .parent_chain_root(parent_chain_root)
            .build();

        Ok(tip_header)
```

**File:** sync/src/relayer/mod.rs (L815-818)
```rust
        // If self is in the IBD state, don't process any relayer message.
        if self.shared.active_chain().is_initial_block_download() {
            return;
        }
```

**File:** shared/src/shared.rs (L382-394)
```rust
    pub fn is_initial_block_download(&self) -> bool {
        // Once this function has returned false, it must remain false.
        if self.ibd_finished.load(Ordering::Acquire) {
            false
        } else if unix_time_as_millis().saturating_sub(self.snapshot().tip_header().timestamp())
            > MAX_TIP_AGE
        {
            true
        } else {
            self.ibd_finished.store(true, Ordering::Release);
            false
        }
    }
```

**File:** util/jsonrpc-types/src/info.rs (L99-106)
```rust
    /// Whether the local node is in IBD, Initial Block Download.
    ///
    /// When a node starts and its chain tip timestamp is far behind the wall clock, it will enter
    /// the IBD until it catches up the synchronization.
    ///
    /// During IBD, the local node only synchronizes the chain with one selected remote node and
    /// stops responding the most P2P requests.
    pub is_initial_block_download: bool,
```
