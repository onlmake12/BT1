### Title
Single-Miner Block Timestamp Manipulation Shifts Block Median Time, Enabling Premature `since` Time-Lock Satisfaction — (File: `traits/src/header_provider.rs`)

---

### Summary

CKB computes a **block median time** from the last 37 block timestamps. This value is the authoritative clock used to evaluate timestamp-based `since` time-locks on transactions. A miner who mines even a single block in the 37-block window can set that block's timestamp to any value up to `now + ALLOWED_FUTURE_BLOCKTIME`, shifting the median time forward and causing time-locked transactions to become spendable earlier than real wall-clock time permits.

---

### Finding Description

`block_median_time` in `traits/src/header_provider.rs` collects timestamps from the last `median_block_count` (37) ancestor blocks, sorts them, and returns the element at index `timestamps.len() >> 1` (index 18 for a full 37-block window): [1](#0-0) 

The constant is fixed at 37: [2](#0-1) 

`TimestampVerifier` enforces that each new block's timestamp must be strictly greater than the median of its parent's 37-block window, but also permits timestamps up to `now + ALLOWED_FUTURE_BLOCKTIME` in the future: [3](#0-2) 

This means a miner building a block may legally set its timestamp to any value in the range `(previous_median, now + ALLOWED_FUTURE_BLOCKTIME]`. By choosing the maximum allowed future timestamp, the miner inserts an artificially inflated value into the 37-element sorted array. Depending on where this inflated value lands relative to the other 36 timestamps, it can push the median (index 18) upward — exactly the same mechanism as the single-oracle median manipulation described in the external report.

`SinceVerifier` uses this same `block_median_time` as the authoritative current time when evaluating absolute and relative timestamp-based `since` locks: [4](#0-3) 

For absolute timestamp locks: [5](#0-4) 

For relative timestamp locks: [6](#0-5) 

---

### Impact Explanation

A miner who mines one block in the 37-block window and sets its timestamp to `now + ALLOWED_FUTURE_BLOCKTIME` can advance the median time by up to that future-time allowance. Any transaction whose `since` timestamp condition falls within the gap between the honest median and the inflated median becomes immediately spendable in the miner's own next block — before real wall-clock time has reached the intended unlock point. This allows the miner to:

1. **Prematurely spend time-locked outputs** — e.g., unlock a payment channel or DAO withdrawal before the agreed-upon time.
2. **Raise the minimum timestamp floor for subsequent blocks** — honest miners using accurate wall-clock times may find their blocks rejected if the inflated median exceeds their real `now`, causing chain disruption.

---

### Likelihood Explanation

Any miner who successfully mines at least one block in a 37-block window — proportional to their share of total hashrate — can execute this manipulation. A miner with even ~3% of hashrate has a meaningful probability of mining one block per window. No special privilege, key, or social engineering is required; the attacker only needs to be a block producer, which is an unprivileged role open to any participant.

---

### Recommendation

**Short term:** Monitor on-chain block timestamps for values significantly ahead of wall-clock time. Alert when any block in the canonical chain carries a timestamp more than a threshold (e.g., 60 seconds) ahead of the previous block's timestamp.

**Long term:** Consider tightening `ALLOWED_FUTURE_BLOCKTIME` to reduce the maximum shift a single block can introduce into the median. Alternatively, evaluate whether the median window size (37) provides sufficient resistance: a larger window reduces the per-block influence on the median. Protocol designers should document the accepted manipulation range explicitly so that `since` time-lock users can account for it in their security models.

---

### Proof of Concept

1. Observe the current 37-block window. Let the sorted timestamps be `T[0] < T[1] < … < T[36]`. The current median is `T[18]`.
2. A miner mines block `B` and sets `B.timestamp = now + ALLOWED_FUTURE_BLOCKTIME` (the maximum permitted value per `TimestampVerifier`).
3. `B` enters the window. The new sorted array of 37 timestamps now includes this inflated value. If the inflated value is larger than `T[18]`, the new median shifts upward — potentially to `T[19]` or higher, depending on the gap.
4. In the miner's immediately following block, `SinceVerifier` calls `block_median_time` using the window that includes `B`. The returned median is now the inflated value.
5. Any transaction with an absolute `since` timestamp ≤ inflated median, but > honest median, is now accepted as mature and can be included in the miner's block — before real wall-clock time has reached the intended unlock point. [7](#0-6) [8](#0-7)

### Citations

**File:** traits/src/header_provider.rs (L32-50)
```rust
    fn block_median_time(&self, block_hash: &Byte32, median_block_count: usize) -> u64 {
        let mut timestamps: Vec<u64> = Vec::with_capacity(median_block_count);
        let mut block_hash = block_hash.clone();
        for _ in 0..median_block_count {
            let header_fields = self
                .get_header_fields(&block_hash)
                .expect("parent header exist");
            timestamps.push(header_fields.timestamp);
            block_hash = header_fields.parent_hash;

            if header_fields.number == 0 {
                break;
            }
        }

        // return greater one if count is even.
        timestamps.sort_unstable();
        timestamps[timestamps.len() >> 1]
    }
```

**File:** spec/src/consensus.rs (L55-55)
```rust
const MEDIAN_TIME_BLOCK_COUNT: usize = 37;
```

**File:** verification/src/header_verifier.rs (L70-96)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        // skip genesis block
        if self.header.is_genesis() {
            return Ok(());
        }

        let min = self.data_loader.block_median_time(
            &self.header.data().raw().parent_hash(),
            self.median_block_count,
        );
        if self.header.timestamp() <= min {
            return Err(TimestampError::BlockTimeTooOld {
                min,
                actual: self.header.timestamp(),
            }
            .into());
        }
        let max = self.now + ALLOWED_FUTURE_BLOCKTIME;
        if self.header.timestamp() > max {
            return Err(TimestampError::BlockTimeTooNew {
                max,
                actual: self.header.timestamp(),
            }
            .into());
        }
        Ok(())
    }
```

**File:** verification/src/transaction_verifier.rs (L618-630)
```rust
    fn parent_median_time(&self, block_hash: &Byte32) -> u64 {
        let header_fields = self
            .data_loader
            .get_header_fields(block_hash)
            .expect("parent block exist");
        self.block_median_time(&header_fields.parent_hash)
    }

    fn block_median_time(&self, block_hash: &Byte32) -> u64 {
        let median_block_count = self.consensus.median_time_block_count();
        self.data_loader
            .block_median_time(block_hash, median_block_count)
    }
```

**File:** verification/src/transaction_verifier.rs (L651-657)
```rust
                Some(SinceMetric::Timestamp(timestamp)) => {
                    let parent_hash = self.tx_env.parent_hash();
                    let tip_timestamp = self.block_median_time(&parent_hash);
                    if tip_timestamp < timestamp {
                        return Err((TransactionError::Immature { index }).into());
                    }
                }
```

**File:** verification/src/transaction_verifier.rs (L699-725)
```rust
                Some(SinceMetric::Timestamp(timestamp)) => {
                    // pass_median_time(current_block) starts with tip block, which is the
                    // parent of current block.
                    // pass_median_time(input_cell's block) starts with cell_block_number - 1,
                    // which is the parent of input_cell's block
                    let proposal_window = self.consensus.tx_proposal_window();
                    let parent_hash = self.tx_env.parent_hash();
                    let epoch_number = self.tx_env.epoch_number(proposal_window);
                    let hardfork_switch = self.consensus.hardfork_switch();
                    let base_timestamp = if hardfork_switch
                        .ckb2021
                        .is_block_ts_as_relative_since_start_enabled(epoch_number)
                    {
                        self.data_loader
                            .get_header_fields(&info.block_hash)
                            .expect("header exist")
                            .timestamp
                    } else {
                        self.parent_median_time(&info.block_hash)
                    };
                    let current_median_time = self.block_median_time(&parent_hash);
                    let required_timestamp = base_timestamp
                        .checked_add(timestamp)
                        .ok_or(TransactionError::InvalidSince { index })?;
                    if current_median_time < required_timestamp {
                        return Err((TransactionError::Immature { index }).into());
                    }
```
