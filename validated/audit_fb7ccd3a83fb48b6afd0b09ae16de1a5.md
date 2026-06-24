All six code claims are confirmed against the actual source. Here is the validation result:

---

Audit Report

## Title
Import Process Infinite Busy-Wait on Rejected Last Block — (`util/instrument/src/import.rs`)

## Summary
`read_from_json` records `largest_block_number` unconditionally before dispatching each block for async verification. The termination loop polls `get_block_hash(largest_block_number)` until it returns `Some`, but `COLUMN_INDEX` is only written by `attach_block` when a block is committed to the canonical chain. If the last block fails `non_contextual_verify`, it is never inserted and never attached, so `get_block_hash` returns `None` forever and the unbounded `while … is_none() { sleep(1s) }` loop never exits.

## Finding Description

**Step 1 — sentinel is set unconditionally before dispatch.**

`largest_block_number` is updated to `max(largest_block_number, block.number())` before the block is handed to `asynchronous_process_lonely_block`, regardless of whether it will pass verification. [1](#0-0) 

**Step 2 — `non_contextual_verify` failure causes early return; `insert_block` is never called.**

The default import switch is `Switch::NONE` (set in `ckb-bin/src/setup.rs` lines 242–250), which means `disable_non_contextual()` returns `false` and the condition at line 117–118 evaluates to `true` — so `non_contextual_verify` is always called on a standard import. On failure, the block is marked `BLOCK_INVALID`, the error callback fires, and the function returns. `insert_block` at line 133 is never reached. [2](#0-1) 

**Step 3 — `insert_block` does not write `COLUMN_INDEX`.**

`StoreTransaction::insert_block` writes `COLUMN_BLOCK_HEADER`, `COLUMN_BLOCK_UNCLE`, `COLUMN_NUMBER_HASH`, `COLUMN_BLOCK_PROPOSAL_IDS`, and `COLUMN_BLOCK_BODY` — never `COLUMN_INDEX`. [3](#0-2) 

**Step 4 — Only `attach_block` writes `COLUMN_INDEX`.**

A block that never passed `insert_block` → `orphan_broker` → `attach_block` is never in `COLUMN_INDEX`. [4](#0-3) 

**Step 5 — `get_block_hash` reads `COLUMN_INDEX` exclusively.** [5](#0-4) 

**Step 6 — termination loop has no timeout and no error exit.**

There is no deadline, no channel to receive a rejection signal, and no break condition other than `get_block_hash` returning `Some`. The process hangs indefinitely. [6](#0-5) 

## Impact Explanation
The `ckb import` CLI process never returns. The operator cannot complete chain bootstrap or re-import without issuing `SIGKILL`. This is a local command line hang, matching the allowed impact: **Note (0–500 points) — Any local command line crash**.

## Likelihood Explanation
The attack surface is the documented `ckb import <file>` bootstrap path. An attacker who can influence the JSONL file — via a malicious export server, a tampered download, or a man-in-the-middle on an unverified HTTP source — can trigger the hang by appending a single crafted line with an invalid PoW nonce as the last block. No privileged access, key material, or majority hashpower is required. The trigger is deterministic and repeatable. Note that `--skip-all-verify` (`Switch::DISABLE_ALL`) bypasses `non_contextual_verify` and would not trigger the hang; the default invocation (`Switch::NONE`) and `--skip-script-verify` (`Switch::DISABLE_SCRIPT`) are both vulnerable. [7](#0-6) 

## Recommendation
Replace the unconditional sentinel update with tracking only successfully committed blocks, or add a bounded retry with a hard timeout and an error return. The simplest correct fix is to move the `largest_block_number` update into the verify callback when `verify_result` is `Ok`, or to add a deadline to the wait loop that returns `Err` if the sentinel block never appears in `COLUMN_INDEX` within a configurable window (e.g., 60 seconds after the last block was dispatched).

## Proof of Concept
1. Export N valid blocks from a running devnet node to `valid.jsonl`.
2. Append one line: a JSON block with `block_number = N+1`, valid parent hash, but `nonce = 0x0` (guaranteed `BlockVerifier` PoW failure).
3. Run `ckb import valid.jsonl`.
4. Observe: after all N+1 blocks are dispatched, the process prints `"Error verifying block: …"` for the last block and then stalls — `get_block_hash(N+1)` always returns `None`.
5. After 60+ seconds the process has not exited; `kill -9` is required.

### Citations

**File:** util/instrument/src/import.rs (L192-192)
```rust
                largest_block_number = largest_block_number.max(block.number());
```

**File:** util/instrument/src/import.rs (L224-231)
```rust
        while self
            .shared
            .snapshot()
            .get_block_hash(largest_block_number)
            .is_none()
        {
            std::thread::sleep(std::time::Duration::from_secs(1));
        }
```

**File:** chain/src/chain_service.rs (L117-130)
```rust
        if lonely_block.switch().is_none()
            || matches!(lonely_block.switch(), Some(switch) if !switch.disable_non_contextual())
        {
            let result = self.non_contextual_verify(lonely_block.block());
            if let Err(err) = result {
                error!(
                    "block {}-{} verify failed: {:?}",
                    block_number, block_hash, err
                );
                self.shared
                    .insert_block_status(lonely_block.block().hash(), BlockStatus::BLOCK_INVALID);
                lonely_block.execute_callback(Err(err));
                return;
            }
```

**File:** store/src/transaction.rs (L172-210)
```rust
    pub fn insert_block(&self, block: &BlockView) -> Result<(), Error> {
        let hash = block.hash();
        let header = Into::<packed::HeaderView>::into(block.header());
        let uncles = Into::<packed::UncleBlockVecView>::into(block.uncles());
        let proposals = block.data().proposals();
        let txs_len: packed::Uint32 = (block.transactions().len() as u32).into();
        self.insert_raw(COLUMN_BLOCK_HEADER, hash.as_slice(), header.as_slice())?;
        self.insert_raw(COLUMN_BLOCK_UNCLE, hash.as_slice(), uncles.as_slice())?;
        if let Some(extension) = block.extension() {
            self.insert_raw(
                COLUMN_BLOCK_EXTENSION,
                hash.as_slice(),
                extension.as_slice(),
            )?;
        }
        self.insert_raw(
            COLUMN_NUMBER_HASH,
            packed::NumberHash::new_builder()
                .number(block.number())
                .block_hash(hash.clone())
                .build()
                .as_slice(),
            txs_len.as_slice(),
        )?;
        self.insert_raw(
            COLUMN_BLOCK_PROPOSAL_IDS,
            hash.as_slice(),
            proposals.as_slice(),
        )?;
        for (index, tx) in block.transactions().into_iter().enumerate() {
            let key = packed::TransactionKey::new_builder()
                .block_hash(hash.clone())
                .index(index)
                .build();
            let tx_data = Into::<packed::TransactionView>::into(tx);
            self.insert_raw(COLUMN_BLOCK_BODY, key.as_slice(), tx_data.as_slice())?;
        }
        Ok(())
    }
```

**File:** store/src/transaction.rs (L271-279)
```rust
        self.insert_raw(COLUMN_INDEX, block_number.as_slice(), block_hash.as_slice())?;
        for uncle in block.uncles().into_iter() {
            self.insert_raw(
                COLUMN_UNCLES,
                uncle.hash().as_slice(),
                Into::<packed::HeaderView>::into(uncle.header()).as_slice(),
            )?;
        }
        self.insert_raw(COLUMN_INDEX, block_hash.as_slice(), block_number.as_slice())
```

**File:** store/src/store.rs (L266-270)
```rust
    fn get_block_hash(&self, number: BlockNumber) -> Option<packed::Byte32> {
        let block_number: packed::Uint64 = number.into();
        self.get(COLUMN_INDEX, block_number.as_slice())
            .map(|raw| packed::Byte32Reader::from_slice_should_be_ok(raw.as_ref()).to_entity())
    }
```

**File:** ckb-bin/src/setup.rs (L242-250)
```rust
        let switch = {
            if matches.get_flag(cli::ARG_SKIP_ALL_VERIFY) {
                Switch::DISABLE_ALL
            } else if matches.get_flag(cli::ARG_SKIP_SCRIPT_VERIFY) {
                Switch::DISABLE_SCRIPT
            } else {
                Switch::NONE
            }
        };
```
