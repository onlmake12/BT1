### Title
Missing `BLOCK_INVALID` Duplicate Guard in Header Acceptance Allows Repeated Re-validation — (File: `sync/src/synchronizer/headers_process.rs`)

### Summary
`HeaderAcceptor::accept` guards against re-processing a header that is already `HEADER_VALID`, but contains an explicit developer-acknowledged FIXME noting the absence of an equivalent guard for `BLOCK_INVALID`. An unprivileged remote peer can therefore send the same previously-rejected header repeatedly and force the node to re-execute all validation sub-checks on every delivery, with no early-exit path.

### Finding Description
In `sync/src/synchronizer/headers_process.rs`, the `accept` method reads the current block status and short-circuits only for `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus