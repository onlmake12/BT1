### Title
`GAS_OVERHEAD` Constant Underestimates Actual Pre-Execution Gas Cost, Causing Keeper Underpayment in `_processFeesAndPayKeeper` - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

The `Scheduler` contract uses a hardcoded `GAS_OVERHEAD = 30_000` constant to compensate keepers for the gas costs incurred *before* `startGas = gasleft()` is captured at the top of `updatePriceFeeds`. For real Pyth price update calls, the actual pre-execution cost (base transaction cost + calldata cost for large Wormhole-signed update blobs) routinely exceeds 30,000 gas, causing keepers to be systematically underpaid and making it economically irrational to service subscriptions with realistic update data.

### Finding Description

In `Scheduler.updatePriceFeeds`, the gas checkpoint is taken at the very first line:

```solidity
function updatePriceFeeds(uint256 subscriptionId, bytes[] calldata updateData) external override {
    uint256 startGas = gasleft();
    ...
    _processFeesAndPayKeeper(status, startGas, params.priceIds.length);
}
``` [1](#0-0) 

Inside `_processFeesAndPayKeeper`, the keeper reimbursement is:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
``` [2](#0-1) 

`GAS_OVERHEAD` is defined as:

```solidity
/// Fixed gas overhead component used in keeper fee calculation.
/// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
uint256 public constant GAS_OVERHEAD = 30000;
``` [3](#0-2) 

`GAS_OVERHEAD` must cover all gas paid by the keeper *before* the EVM reaches the `gasleft()` call — specifically:

| Cost component | Gas |
|---|---|
| Base transaction cost | 21,000 |
| Calldata: function selector (4 bytes) | ~64 |
| Calldata: `subscriptionId` (32 bytes, mostly zero) | ~32 |
| Calldata: ABI encoding overhead for `bytes[]` | ~128 |
| Calldata: actual Pyth/Wormhole update blob | **variable** |

A single Pyth price update blob (Wormhole-signed accumulator update) is typically **1–5 KB** of mostly non-zero bytes. At 16 gas per non-zero byte, a 2 KB blob costs ~32,768 gas in calldata alone. Adding the 21,000 base cost gives **~53,768 gas** of pre-execution overhead — nearly **1.8× the 30,000 `GAS_OVERHEAD`**.

For subscriptions with `MAX_PRICE_IDS_PER_SUBSCRIPTION = 255` feeds, the update blob can be tens of KB, making the underpayment proportionally larger. [4](#0-3) 

Additionally, `_processFeesAndPayKeeper` itself consumes gas *after* the `gasleft()` measurement (the `msg.sender.call{value: totalKeeperFee}("")` ETH transfer and the `PricesUpdated` event emission), which is also not reimbursed. [5](#0-4) 

The existing test `testUpdatePriceFeedsPaysKeeperCorrectly` explicitly acknowledges the underpayment but treats it as expected behavior:

```solidity
// The real cost is more because of the gas used in the updatePriceFeeds function
uint256 minKeeperFee = (scheduler.GAS_OVERHEAD() * gasPrice) + ...
assertGt(totalFeeDeducted, minKeeperFee + mockPythFee, ...)
``` [6](#0-5) 

However, the test uses mock Pyth data with negligible calldata size. In production, the calldata cost dominates and is not covered.

### Impact Explanation

Keepers are underpaid for their actual gas expenditure on every `updatePriceFeeds` call involving real Pyth update data. The underpayment scales with the size of `updateData`:

- For a single feed with a 2 KB update blob: keeper loses ~(53,768 − 30,000) × `tx.gasprice` ≈ 23,768 × `tx.gasprice` per call.
- At 10 gwei gas price: ~237,680 wei (~$0.0005) per call — small individually but significant at scale.
- For subscriptions with many feeds (e.g., 10 feeds × 2 KB = 20 KB calldata): ~(341,000 − 30,000) × `tx.gasprice` per call.

The `singleUpdateKeeperFeeInWei` is a per-feed flat fee that does not scale with calldata size, so it cannot reliably compensate for this underpayment across varying subscription sizes and gas prices. Rational keepers will avoid servicing subscriptions where the underpayment exceeds the `singleUpdateKeeperFeeInWei` surplus, causing those subscriptions to go stale.

### Likelihood Explanation

This affects every `updatePriceFeeds` call with real Pyth update data. Any keeper calling `updatePriceFeeds` with a realistic Wormhole-signed accumulator update (the only valid input in production) will trigger the underpayment. No special conditions are required — it is a structural miscalibration present in every execution.

### Recommendation

Replace the static `GAS_OVERHEAD` with a dynamic calculation that accounts for calldata size:

```solidity
// Approximate calldata cost: 4 gas per zero byte, 16 gas per non-zero byte
// Use a conservative 16 gas/byte estimate for all calldata
uint256 calldataCost = msg.data.length * 16;
uint256 gasCost = (startGas - gasleft() + 21_000 + calldataCost) * tx.gasprice;
```

Alternatively, increase `GAS_OVERHEAD` to a value that covers the worst-case pre-execution cost for the maximum supported `updateData` size, or make it a configurable admin parameter.

### Proof of Concept

For a subscription with 1 price feed and a realistic 2 KB Pyth update blob:

1. Keeper calls `updatePriceFeeds(subscriptionId, updateData)` where `updateData[0]` is a 2,048-byte Wormhole accumulator update.
2. Calldata cost: `(4 + 32 + 32 + 32 + 32 + 2048) × 16 ≈ 34,880 gas` (non-zero bytes).
3. Total pre-execution cost: `21,000 + 34,880 = 55,880 gas`.
4. `GAS_OVERHEAD = 30,000` covers only `30,000 / 55,880 ≈ 53.7%` of the actual pre-execution cost.
5. Keeper is underpaid by `25,880 × tx.gasprice` on every call.
6. At 50 gwei: `25,880 × 50e9 = 1,294,000 gwei ≈ 0.0013 ETH` per call — exceeding typical `singleUpdateKeeperFeeInWei` values. [7](#0-6) [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-279)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
        uint256 startGas = gasleft();
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L840-864)
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
    }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L7-8)
```text
    /// Maximum number of price feeds per subscription
    uint8 public constant MAX_PRICE_IDS_PER_SUBSCRIPTION = 255;
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L27-29)
```text
    /// Fixed gas overhead component used in keeper fee calculation.
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L1343-1353)
```text
        // Calculate minimum keeper fee (overhead + feed-specific fee)
        // The real cost is more because of the gas used in the updatePriceFeeds function
        uint256 minKeeperFee = (scheduler.GAS_OVERHEAD() * gasPrice) +
            (uint256(scheduler.getSingleUpdateKeeperFeeInWei()) *
                params.priceIds.length);

        assertGt(
            totalFeeDeducted,
            minKeeperFee + mockPythFee,
            "Total fee deducted should be greater than the sum of keeper fee and Pyth fee (since gas usage of updatePriceFeeds is not accounted for)"
        );
```
