### Title
Fixed `GAS_OVERHEAD` Constant Fails to Account for Variable `updateData` Calldata Cost, Causing Keeper Underpayment - (File: `target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol`)

---

### Summary

`Scheduler._processFeesAndPayKeeper()` reimburses keepers using `(startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice`, where `GAS_OVERHEAD = 30_000` is a fixed constant intended to cover the EVM intrinsic transaction cost. Because `startGas = gasleft()` is captured inside the function body — after the EVM has already deducted the intrinsic cost (21,000 base gas + variable calldata gas) — the `GAS_OVERHEAD` must cover both. However, `updateData` is a variable-length `bytes[] calldata` array whose calldata cost scales with the number of price feeds in the subscription (up to 255). For subscriptions with many feeds, the calldata cost alone can far exceed 30,000 gas, causing keepers to be systematically underpaid and subscription owners to receive updates at a discount.

---

### Finding Description

In `Scheduler.updatePriceFeeds()`, `startGas` is captured at the very first line of the function:

```solidity
function updatePriceFeeds(
    uint256 subscriptionId,
    bytes[] calldata updateData
) external override {
    uint256 startGas = gasleft();   // <-- captured AFTER intrinsic cost is paid
    ...
    _processFeesAndPayKeeper(status, startGas, params.priceIds.length);
}
``` [1](#0-0) 

In `_processFeesAndPayKeeper`, the keeper reimbursement is:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
``` [2](#0-1) 

`GAS_OVERHEAD` is defined as:

```solidity
/// Fixed gas overhead component used in keeper fee calculation.
/// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
uint256 public constant GAS_OVERHEAD = 30000;
``` [3](#0-2) 

The EVM charges the intrinsic transaction cost **before** any opcode executes, so `gasleft()` at line 279 already has the intrinsic cost deducted. The intrinsic cost is:

- **21,000 gas** (base transaction cost)
- **4 gas per zero byte / 16 gas per non-zero byte** of calldata (the `updateData` array)

`GAS_OVERHEAD = 30,000` leaves only 9,000 gas to cover the entire calldata cost of `updateData`. A single Pyth accumulator update for one price feed is approximately 400–600 bytes. For a subscription with many feeds, the `updateData` array grows proportionally. At 16 gas per non-zero byte:

| Price Feeds | Approx. `updateData` size | Calldata gas | Total intrinsic | Shortfall vs. 30,000 |
|---|---|---|---|---|
| 1 | ~500 bytes | ~8,000 | ~29,000 | ~0 (marginal) |
| 5 | ~2,500 bytes | ~40,000 | ~61,000 | ~31,000 |
| 20 | ~10,000 bytes | ~160,000 | ~181,000 | ~151,000 |
| 50 | ~25,000 bytes | ~400,000 | ~421,000 | ~391,000 |

The maximum allowed is `MAX_PRICE_IDS_PER_SUBSCRIPTION = 255`. [4](#0-3) 

The `startGas - gasleft()` term captures only the gas consumed by the function body (storage reads, Pyth parsing, etc.), not the intrinsic cost. The shortfall is borne entirely by the keeper.

---

### Impact Explanation

Keepers are underpaid for every `updatePriceFeeds` call on subscriptions with more than a handful of price feeds. The subscription's `balanceInWei` is debited less than the keeper's true transaction cost. Subscription owners effectively receive price updates at a discount — paying less ETH per update than the actual gas expenditure. For subscriptions near the 255-feed maximum, the underpayment per call can be hundreds of thousands of gas units multiplied by `tx.gasprice`, making it economically irrational for keepers to service those subscriptions. This degrades liveness for large subscriptions and constitutes a fee accounting error where the protocol systematically undercharges subscribers.

---

### Likelihood Explanation

Any unprivileged user can create a subscription with many price feeds via `createSubscription`. The minimum balance check (`getMinimumBalance`) does not account for calldata costs, so a subscription with 50+ feeds can be created and funded at the minimum balance while causing keepers to be underpaid on every update. The entry path requires no special privileges — only a call to `createSubscription` with a large `priceIds` array. [5](#0-4) 

---

### Recommendation

Replace the fixed `GAS_OVERHEAD` with a formula that accounts for the variable calldata size of `updateData`. One approach is to compute the calldata cost on-chain:

```solidity
// Compute calldata gas cost for updateData
uint256 calldataGas = 0;
for (uint256 i = 0; i < updateData.length; i++) {
    calldataGas += updateData[i].length * 16; // conservative: all non-zero bytes
}
uint256 intrinsicGas = 21000 + calldataGas + FIXED_CALLDATA_OVERHEAD;
uint256 gasCost = (startGas - gasleft() + intrinsicGas) * tx.gasprice;
```

Alternatively, pass `updateData.length` (total byte count) into `_processFeesAndPayKeeper` and compute `16 * totalBytes + 21000` as the overhead. The constant `FIXED_CALLDATA_OVERHEAD` should cover the ABI-encoded function selector, `subscriptionId`, and array length headers.

---

### Proof of Concept

1. Deploy `Scheduler` with `singleUpdateKeeperFeeInWei = 0` and `minimumBalancePerFeed` set to a small value.
2. Create a subscription with 50 price feeds. Fund it at the minimum balance.
3. Set `vm.txGasPrice(10 gwei)`.
4. Call `updatePriceFeeds` with valid `updateData` for all 50 feeds (~25,000 bytes).
5. Observe that the keeper receives `(startGas - gasleft() + 30000) * 10 gwei`.
6. Compute the actual transaction cost: `(21000 + 25000 * 16) * 10 gwei = 4,210,000 * 10 gwei = 0.04210 ETH`.
7. The keeper's reimbursement covers only the function body gas + 30,000 overhead, missing ~391,000 gas × 10 gwei = **~0.00391 ETH per call** in underpayment — and this scales linearly with feed count and gas price. [6](#0-5) [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L32-45)
```text
    function createSubscription(
        SchedulerStructs.SubscriptionParams memory subscriptionParams
    ) external payable override returns (uint256 subscriptionId) {
        _validateSubscriptionParams(subscriptionParams);

        // Calculate minimum balance required for this subscription
        uint256 minimumBalance = this.getMinimumBalance(
            uint8(subscriptionParams.priceIds.length)
        );

        // Ensure enough funds were provided
        if (msg.value < minimumBalance) {
            revert SchedulerErrors.InsufficientBalance();
        }
```

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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L8-8)
```text
    uint8 public constant MAX_PRICE_IDS_PER_SUBSCRIPTION = 255;
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L27-29)
```text
    /// Fixed gas overhead component used in keeper fee calculation.
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
```
