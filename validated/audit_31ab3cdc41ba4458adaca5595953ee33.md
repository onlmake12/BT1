### Title
Keeper Gas Compensation Underestimates Calldata Overhead, Making `updatePriceFeeds` Unprofitable at High Gas Prices — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol` / `target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol`)

### Summary

The `GAS_OVERHEAD` constant (30,000 gas) used in `_processFeesAndPayKeeper` to compensate keepers for transaction overhead does not account for the variable calldata cost of `updateData`. For subscriptions with many price feeds or large update payloads, the actual transaction overhead (base cost + calldata) can far exceed 30,000 gas. This causes keepers to be systematically underpaid, making `updatePriceFeeds` unprofitable at elevated gas prices and causing subscriptions' price feeds to go stale.

### Finding Description

In `Scheduler.sol`, `updatePriceFeeds` captures `startGas = gasleft()` at the very start of the function body. The keeper compensation is calculated in `_processFeesAndPayKeeper` as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
```

`startGas - gasleft()` captures only gas consumed *inside* the function body. It does **not** capture:
1. The EVM base transaction cost (21,000 gas on Ethereum).
2. Calldata costs: 4 gas per zero byte, 16 gas per non-zero byte — paid before any EVM opcode executes.

`GAS_OVERHEAD = 30,000` is a hardcoded constant meant to cover both of these. However:

- Base cost alone: 21,000 gas.
- Calldata for a 2-feed subscription with ~1 KB of `updateData`: ~16,000 gas.
- Total actual overhead: ~37,000 gas → **exceeds `GAS_OVERHEAD` by ~7,000 gas**.
- For a 10-feed subscription with ~5 KB of `updateData`: ~80,000 gas calldata + 21,000 base = ~101,000 gas → **exceeds `GAS_OVERHEAD` by ~71,000 gas**.

`GAS_OVERHEAD` is declared as an immutable `constant` in `SchedulerConstants.sol` and cannot be adjusted by the admin. The only adjustable parameter is `singleUpdateKeeperFeeInWei`, which is a flat per-feed fee and does not scale with calldata size.

When the underpayment `(actual_overhead − GAS_OVERHEAD) × tx.gasprice` exceeds `singleUpdateKeeperFeeInWei × numPriceIds`, the keeper's net profit is negative. Rational keepers will stop calling `updatePriceFeeds`, leaving subscriptions' price feeds stale.

### Impact Explanation

Subscriptions whose price feeds go stale due to unprofitable keeper economics will silently serve outdated prices to any protocol reading them via `getPricesNoOlderThan` or `getEmaPricesNoOlderThan`. Protocols that rely on Pyth Scheduler for on-chain price data (e.g., lending protocols, perpetuals, options) will receive stale prices, potentially enabling mispriced liquidations, bad debt, or arbitrage against the stale feed. The subscription remains listed as active in `activeSubscriptionIds`, so keepers continue to attempt updates, waste gas on reverts, and the stale state persists until the subscription manager manually tops up or deactivates.

### Likelihood Explanation

The Ethereum mainnet regularly experiences gas price spikes (100–1000+ gwei during congestion). A subscription with 10 price feeds and typical Wormhole VAA update data (~500 bytes per feed = 5 KB total) incurs ~80,000 gas in calldata alone. At 100 gwei, the underpayment is ~0.007 ETH per update. If `singleUpdateKeeperFeeInWei` is set to a modest value (e.g., 0.0001 ETH per feed × 10 feeds = 0.001 ETH), the keeper loses ~0.006 ETH per call. Keepers operating at scale will rationally skip such subscriptions. This is not a theoretical edge case — it is a predictable outcome during any gas price spike.

### Recommendation

1. Replace the hardcoded `GAS_OVERHEAD` constant with a governance-adjustable parameter so the admin can tune it as calldata costs evolve.
2. Alternatively, measure calldata size explicitly and add a per-byte calldata cost component to the keeper fee calculation.
3. Consider adding a minimum keeper profitability check before executing the update, reverting early if the subscription balance cannot cover the estimated full cost (including calldata overhead), rather than discovering the shortfall only at `_processFeesAndPayKeeper`.

### Proof of Concept

**Setup:**
- Subscription with 10 price feeds, each update element ~500 bytes → `updateData` ≈ 5 KB.
- `GAS_OVERHEAD = 30,000` (constant, non-adjustable).
- `singleUpdateKeeperFeeInWei = 100,000 gwei` (0.0001 ETH per feed × 10 = 0.001 ETH).
- Gas price = 100 gwei.

**Actual transaction overhead:**
- Base cost: 21,000 gas.
- Calldata (5,000 bytes, ~80% non-zero): 5,000 × 0.8 × 16 + 5,000 × 0.2 × 4 = 64,000 + 4,000 = 68,000 gas.
- Total actual overhead: 89,000 gas.

**Keeper fee paid by contract:**
- `gasCost = (startGas - gasleft() + 30,000) × 100 gwei` — captures in-function gas + 30,000 overhead.
- `keeperSpecificFee = 0.001 ETH`.
- Underpayment for overhead: `(89,000 − 30,000) × 100 gwei = 59,000 × 100 gwei = 0.0059 ETH`.
- Net keeper loss: `0.0059 ETH − 0.001 ETH = −0.0049 ETH per call`.

**Result:** Keeper loses ~0.005 ETH per update. Rational keepers stop calling `updatePriceFeeds`. The subscription's price feeds go stale while the subscription remains listed as active.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L27-29)
```text
    /// Fixed gas overhead component used in keeper fee calculation.
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L279-279)
```text
        uint256 startGas = gasleft();
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L840-863)
```text
    function _processFeesAndPayKeeper(
        SchedulerStructs.SubscriptionStatus storage status,
        uint256 startGas,
        uint256 numPriceIds
    ) internal {
        // Calculate fee components
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
        uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) *
            numPriceIds;
        uint256 totalKeeperFee = gasCost + keeperSpecificFee;

        // Check balance
        if (status.balanceInWei < totalKeeperFee) {
            revert SchedulerErrors.InsufficientBalance();
        }

        status.balanceInWei -= totalKeeperFee;
        status.totalSpent += totalKeeperFee;

        // Pay keeper and update status
        (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
        if (!sent) {
            revert SchedulerErrors.KeeperPaymentFailed();
        }
```
