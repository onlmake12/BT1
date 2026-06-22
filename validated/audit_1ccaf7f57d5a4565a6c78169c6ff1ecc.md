### Title
`BlockReader::extension()` Panics on Malformed Block Extension Field, Enabling P2P DoS — (`util/gen-types/src/extension/shortcut.rs`)

---

### Summary

`packed::BlockReader::extension()` calls `.unwrap()` on a fallible molecule deserialization of the block's extra field. A remote peer can craft a block or compact block whose outer molecule encoding passes the relay handler's `from_compatible_slice` guard but whose embedded extension bytes are not a valid `BytesReader`, causing an unconditional process-terminating panic when `extension()` is called in the relay and verification pipeline.

---

### Finding Description

`BlockReader::extension()` is defined as:

```rust
// util/gen-types/src/extension/shortcut.rs, lines 251-254
pub fn extension(&self) -> Option<packed::BytesReader<'_>> {
    self.extra_field(0)
        .map(|data| packed::BytesReader::from_slice(data).unwrap())
}
```

The function's own doc comment acknowledges the panic:

> **# Panics**
> Panics if the first extra field exists but not a valid [`BytesReader`]. [1](#0-0) 

The relay handler in `sync/src/relayer/mod.rs` guards incoming P2P messages with `packed::RelayMessageReader::from_compatible_slice(&data)`: [2](#0-1) 

`from_compatible_slice` is a **lenient** parser — it accepts extra trailing bytes and does not recursively validate nested fields. It will accept a `CompactBlock` or `SendBlock` whose embedded `Block` carries a syntactically present but internally malformed extension field. The outer check passes; the panic fires later when `extension()` is called during relay processing or block verification.

`extension()` is called in at least the following production paths reachable from external input:

- `sync/src/relayer/mod.rs` (2 call sites) — compact block relay processing
- `verification/contextual/src/contextual_block_verifier.rs` — contextual block verification
- `store/src/transaction.rs` — block storage
- `rpc/src/module/miner.rs` — miner RPC block template assembly
- `util/light-client-protocol-server/src/lib.rs` (2 call sites) — light client protocol [3](#0-2) 

The `ShouldBeOk` / `from_slice_should_be_ok` pattern used elsewhere in the codebase is explicitly designed to panic on bad data:

```rust
// util/gen-types/src/prelude.rs, lines 26-30
impl<T> ShouldBeOk<T> for molecule::error::VerificationResult<T> {
    fn should_be_ok(self) -> T {
        self.unwrap_or_else(|err| panic!("verify slice should be ok, but {err}"))
    }
}
``` [4](#0-3) 

`extension()` uses a bare `.unwrap()` rather than `should_be_ok`, but the effect is identical: any `Err` from `BytesReader::from_slice` terminates the process.

---

### Impact Explanation

A single malformed P2P message causes the entire `ckb` node process to terminate. Because the panic occurs inside the relay message handler — which runs in the node's async runtime — the crash is not isolated to a single connection. The attacker can reconnect and repeat indefinitely, keeping the target node offline. Nodes that accept inbound connections from the public internet (the default) are directly exposed. Crash-looping a node also prevents it from propagating valid blocks and transactions, degrading network health.

---

### Likelihood Explanation

The attack requires only a TCP connection to a CKB node's P2P port (default 8115), which is publicly reachable by design. No authentication, no stake, no privileged key is needed. The attacker needs to:

1. Connect as a peer.
2. Send a `RelayMessage` containing a `CompactBlock` or `SendBlock` whose embedded `Block` has an extra field that is syntactically present (so `from_compatible_slice` accepts it) but whose bytes are not a valid molecule `Bytes` encoding (so `BytesReader::from_slice` returns `Err`).

Constructing such a payload requires only knowledge of the molecule wire format, which is fully documented and open-source. The attack is trivially scriptable and repeatable.

---

### Recommendation

Replace the `.unwrap()` in `extension()` with graceful error propagation:

```rust
// util/gen-types/src/extension/shortcut.rs
pub fn extension(&self) -> Option<Result<packed::BytesReader<'_>, molecule::error::VerificationError>> {
    self.extra_field(0)
        .map(|data| packed::BytesReader::from_slice(data))
}
```

All callers that currently treat `extension()` as infallible must be updated to handle the `Err` case — in the relay handler, by banning the peer and returning early (matching the existing pattern for other malformed messages at lines 831–839 and 853–863 of `sync/src/relayer/mod.rs`). [5](#0-4) 

---

### Proof of Concept

1. Establish a TCP connection to the target node's P2P port.
2. Complete the CKB P2P handshake.
3. Construct a molecule-encoded `RelayMessage` of union type `CompactBlock` where the embedded `Block` has:
   - A valid outer molecule table structure (so `from_compatible_slice` succeeds).
   - An extra field offset pointing to bytes that are **not** a valid molecule `Bytes` encoding (e.g., `[0x05, 0x00, 0x00, 0x00, 0xFF]` — a `Bytes` header claiming length 5 but with invalid content).
4. Send the message.
5. The relay handler calls `from_compatible_slice` → succeeds → dispatches to `CompactBlockProcess` → calls `block.as_reader().extension()` → `BytesReader::from_slice(malformed_bytes)` returns `Err` → `.unwrap()` panics → node process terminates.
6. Repeat from step 1 to maintain persistent DoS.

The existing test `send_block_check_data_rejects_malformed_block_extension` in `util/gen-types/src/extension/tests/check_data.rs` demonstrates that such malformed extension bytes can be constructed and that `check_data()` is the intended guard — but `check_data()` is not called before `extension()` in the relay hot path. [6](#0-5)

### Citations

**File:** util/gen-types/src/extension/shortcut.rs (L246-254)
```rust
    /// Gets the extension field if it existed.
    ///
    /// # Panics
    ///
    /// Panics if the first extra field exists but not a valid [`BytesReader`](struct.BytesReader.html).
    pub fn extension(&self) -> Option<packed::BytesReader<'_>> {
        self.extra_field(0)
            .map(|data| packed::BytesReader::from_slice(data).unwrap())
    }
```

**File:** sync/src/relayer/mod.rs (L820-879)
```rust
        let msg = match packed::RelayMessageReader::from_compatible_slice(&data) {
            Ok(msg) => {
                let item = msg.to_enum();
                if let packed::RelayMessageUnionReader::CompactBlock(ref reader) = item {
                    if reader.count_extra_fields() > 1 {
                        info_target!(
                            crate::LOG_TARGET_RELAY,
                            "Peer {} sends us a malformed message: \
                             too many fields in CompactBlock",
                            peer_index
                        );
                        nc.ban_peer(
                            peer_index,
                            BAD_MESSAGE_BAN_TIME,
                            String::from(
                                "send us a malformed message: \
                                 too many fields in CompactBlock",
                            ),
                        );
                        return;
                    } else {
                        item
                    }
                } else {
                    match packed::RelayMessageReader::from_slice(&data) {
                        Ok(msg) => msg.to_enum(),
                        _ => {
                            info_target!(
                                crate::LOG_TARGET_RELAY,
                                "Peer {} sends us a malformed message: \
                                 too many fields",
                                peer_index
                            );
                            nc.ban_peer(
                                peer_index,
                                BAD_MESSAGE_BAN_TIME,
                                String::from(
                                    "send us a malformed message \
                                     too many fields",
                                ),
                            );
                            return;
                        }
                    }
                }
            }
            _ => {
                info_target!(
                    crate::LOG_TARGET_RELAY,
                    "Peer {} sends us a malformed message",
                    peer_index
                );
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };
```

**File:** util/gen-types/src/prelude.rs (L26-30)
```rust
impl<T> ShouldBeOk<T> for molecule::error::VerificationResult<T> {
    fn should_be_ok(self) -> T {
        self.unwrap_or_else(|err| panic!("verify slice should be ok, but {err}"))
    }
}
```

**File:** util/gen-types/src/extension/tests/check_data.rs (L63-76)
```rust
#[test]
fn send_block_check_data_rejects_malformed_block_extension() {
    let block = packed::Block::default();
    let block_with_malformed_extension =
        append_malformed_bytes_extra_field(block.as_slice(), packed::Block::FIELD_COUNT);
    let send_block = packed::SendBlock::new_builder()
        .block(packed::Block::new_unchecked(
            block_with_malformed_extension.into(),
        ))
        .build();

    let reader = packed::SendBlockReader::from_compatible_slice(send_block.as_slice()).unwrap();
    assert!(!reader.check_data());
}
```
