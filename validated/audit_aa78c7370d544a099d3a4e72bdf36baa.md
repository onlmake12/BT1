### Title
Miner-Controlled Timestamp Causes `epoch_duration_in_milliseconds` to Collapse to Zero, Biasing Difficulty Upward by 2× - (File: `traits/src/epoch_provider.rs`)

---

### Summary

The `get_block_epoch` default implementation in `traits/src/epoch_provider.rs` computes `epoch_duration_in_milliseconds` with a `saturating_sub`. Because CKB block timestamps are not required to be strictly monotonically increasing (only the median-time rule is enforced), a miner who wins the epoch tail block can legally set its timestamp at or below the timestamp of