### Title
`estimate_priority_fee` Panics on Empty Inner Reward Vec, Crashing the Fortuna Keeper — (File: `apps/fortuna/src/eth_utils/eth_gas_oracle.rs`)

---

### Summary

`estimate_priority_fee` in Fortuna's EIP-1559 gas oracle unconditionally indexes `r[0]` on every inner reward vector returned by `eth_feeHistory`. When the RPC returns an empty inner array for a block with no transactions — a documented, normal Ethereum behavior — the indexing panics, crashing the async keeper task and preventing Entropy request fulfillment.

---

### Finding Description

`estimate_priority_fee` maps over the `rewards: Vec<Vec<U256>>` returned by `fee_history`:

```rust
let mut rewards: Vec<U256> = rewards
    .iter()
    .map(|r| r[0])          // ← panics if any inner Vec is empty
    .filter(|r| *r > U256::zero())
    .collect();
``` [1](#0-0) 

The Ethereum JSON-RPC specification for `eth_feeHistory` states that the `rewards` field contains one inner array per block, and that inner array is **empty `[]`** for blocks that contain no transactions. This is not an error condition — it is normal behavior on low-activity networks and L2s. When any such block appears in the fee history window, `r[0]` panics with an index-out-of-bounds error.

The function is called from `estimate_eip1559_fees`, which is invoked by the keeper loop to price every on-chain transaction:

```rust
let fee_history = self.provider.fee_history(...).await?;
let rewards: Vec<Vec<U256>> = fee_history.reward;
let (max_fee_per_gas, max_priority_fee_per_gas) = eip1559_default_estimator(
    base_fee_per_gas, rewards, ...
);
``` [2](#0-1) 

The root cause is structurally identical to the reported `IsPowerOfTwo(0)` bug: a helper function that assumes its input is always non-empty, with no guard for the zero-length case.

---

### Impact Explanation

When the panic fires, the Tokio task running the keeper crashes. The Fortuna keeper is responsible for fulfilling user entropy requests on-chain. A crashed keeper means pending entropy callbacks are never executed, constituting a **denial of service against the Entropy service**. Users who have paid for randomness receive no response.

---

### Likelihood Explanation

Fortuna is explicitly designed for L2 networks (the comment at line 13–17 explains the code exists because L2 fee behavior differs from mainnet). L2 networks routinely produce blocks with no user transactions, especially during low-activity periods. The `fee_estimation_past_blocks` window (configurable, typically several blocks) makes it statistically likely that at least one empty-reward block appears in the history window during normal operation. No attacker action is required; the condition arises naturally. [3](#0-2) 

---

### Recommendation

Guard against empty inner vectors before indexing:

```rust
let mut rewards: Vec<U256> = rewards
    .iter()
    .filter_map(|r| r.first().copied())   // skip blocks with no transactions
    .filter(|r| *r > U256::zero())
    .collect();
```

This mirrors the fix recommended for `IsPowerOfTwo`: add an explicit check for the zero/empty case before proceeding with arithmetic or indexing.

---

### Proof of Concept

1. Configure Fortuna against any L2 RPC endpoint.
2. Ensure the last `fee_estimation_past_blocks` blocks contain at least one block with zero transactions (trivially true on testnets or during low-activity windows).
3. Trigger any entropy request that causes the keeper to call `estimate_eip1559_fees`.
4. The keeper task panics at `r[0]` in `estimate_priority_fee` and terminates.
5. All pending entropy callbacks remain unfulfilled. [1](#0-0)

### Citations

**File:** apps/fortuna/src/eth_utils/eth_gas_oracle.rs (L13-19)
```rust
// The default fee estimation logic in ethers.rs includes some hardcoded constants that do not
// work well in layer 2 networks because it lower bounds the priority fee at 3 gwei.
// Unfortunately this logic is not configurable in ethers.rs.
//
// Thus, this file is copy-pasted from places in ethers.rs with all of the fee constants divided by 1000000.
// See original logic here:
// https://github.com/gakonst/ethers-rs/blob/master/ethers-providers/src/rpc/provider.rs#L452
```

**File:** apps/fortuna/src/eth_utils/eth_gas_oracle.rs (L111-133)
```rust
        let fee_history = self
            .provider
            .fee_history(
                self.fee_estimation_past_blocks,
                EthersBlockNumber::Latest,
                &[self.fee_estimation_reward_percentile],
            )
            .await
            .map_err(|err| GasOracleError::ProviderError(Box::new(err)))?;

        let rewards: Vec<Vec<U256>> = fee_history.reward;

        let (max_fee_per_gas, max_priority_fee_per_gas) = eip1559_default_estimator(
            base_fee_per_gas,
            rewards,
            self.min_reward_samples,
            self.eip1559_fee_estimation_default_priority_fee,
            self.eip1559_fee_estimation_priority_fee_trigger,
            self.eip1559_fee_estimation_threshold_max_change,
            self.surge_threshold_1,
            self.surge_threshold_2,
            self.surge_threshold_3,
        );
```

**File:** apps/fortuna/src/eth_utils/eth_gas_oracle.rs (L195-199)
```rust
    let mut rewards: Vec<U256> = rewards
        .iter()
        .map(|r| r[0])
        .filter(|r| *r > U256::zero())
        .collect();
```
