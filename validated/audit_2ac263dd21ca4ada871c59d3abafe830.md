### Title
Keeper-Controlled `tx.gasprice` Inflates Fee Extraction from Subscription Balances - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `_processFeesAndPayKeeper` function in `Scheduler.sol` calculates the keeper fee using `tx.gasprice`, a value entirely controlled by the transaction sender. Because `updatePriceFeeds` is permissionless (any address can act as a keeper), a malicious keeper can set an arbitrarily high `tx.gasprice` to inflate the `gasCost` component of the fee, draining subscription balances far beyond the legitimate cost of the update.

---

### Finding Description

In `Scheduler.sol`, the internal function `_processFeesAndPayKeeper` computes the total fee to deduct from a subscription and pay to the keeper:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
...
status.balanceInWei -= totalKeeperFee;
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
``` [1](#0-0) 

`tx.gasprice` is the gas price of the current transaction, which is set unilaterally by the caller. There is no cap, no governance-set maximum, and no comparison against a reference price. The `GAS_OVERHEAD` constant is added on top of actual gas consumed, meaning the keeper is always paid for more gas than they actually spend. At a high `tx.gasprice`, this overhead becomes a large extraction vector.

The `updatePriceFeeds` function that calls `_processFeesAndPayKeeper` has no access control — it is designed to be callable by any keeper: [2](#0-1) 

---

### Impact Explanation

A malicious keeper calls `updatePriceFeeds` with `tx.gasprice` set to an extremely high value. The contract computes:

```
gasCost = (actualGasUsed + GAS_OVERHEAD) * tx.gasprice
```

The keeper pays `actualGasUsed * tx.gasprice` in network fees but receives `(actualGasUsed + GAS_OVERHEAD) * tx.gasprice + keeperSpecificFee` from the subscription. The net profit per call is:

```
profit = GAS_OVERHEAD * tx.gasprice + keeperSpecificFee
```

Since `GAS_OVERHEAD > 0`, profit scales linearly with `tx.gasprice`. At `tx.gasprice = 1000 gwei` and `GAS_OVERHEAD = 50,000`, the attacker extracts `0.05 ETH` per update call beyond legitimate costs. Subscription balances are drained at an inflated rate, causing:

- Subscription managers lose deposited ETH disproportionate to actual service rendered.
- Subscriptions are deactivated prematurely due to balance exhaustion.
- The malicious keeper profits from the `GAS_OVERHEAD * tx.gasprice` surplus.

There is no check that `status.balanceInWei - totalKeeperFee >= minimumBalance` inside `_processFeesAndPayKeeper`, so the entire balance can be drained in a single call if `totalKeeperFee` equals the balance. [3](#0-2) 

---

### Likelihood Explanation

- `updatePriceFeeds` is permissionless; any address can act as a keeper.
- Setting `tx.gasprice` to a high value is a standard EVM transaction parameter requiring no special access.
- The attack is profitable whenever `GAS_OVERHEAD * tx.gasprice > 0`, which is always true.
- No existing mitigation (slippage check, gas price cap, or oracle-based reference price) is present in the contract.

---

### Recommendation

1. **Cap `tx.gasprice`**: Introduce a governance-set `maxGasPriceInWei` parameter. In `_processFeesAndPayKeeper`, use `min(tx.gasprice, maxGasPriceInWei)` when computing `gasCost`.
2. **Remove `GAS_OVERHEAD` from the gas-price-multiplied component**: Pay `GAS_OVERHEAD` as a flat fee (not multiplied by `tx.gasprice`) to eliminate the profit incentive from inflating gas price.
3. **Off-chain gas price oracle**: Use a time-weighted or EMA gas price oracle (analogous to the Zokyo auditor's recommendation for Curve) to bound the on-chain gas price used in fee calculations.

---

### Proof of Concept

1. Subscription manager creates a subscription with `balanceInWei = 10 ETH`.
2. Attacker (acting as keeper) calls `updatePriceFeeds(subscriptionId, updateData)` with `tx.gasprice = 10,000 gwei`.
3. Assume `actualGasUsed = 200,000`, `GAS_OVERHEAD = 50,000`:
   - `gasCost = (200,000 + 50,000) * 10,000 gwei = 2.5 ETH`
   - Attacker pays: `200,000 * 10,000 gwei = 2 ETH` in network fees
   - Attacker receives: `2.5 ETH + keeperSpecificFee`
   - **Net profit: `0.5 ETH + keeperSpecificFee` per call**
4. After 4 calls, the subscription's 10 ETH balance is exhausted; the subscription is deactivated.
5. The subscription manager's funds are drained at 5× the legitimate rate. [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L683-684)
```text
    // This function is intentionally public with no access control to allow keepers to discover active subscriptions
    function getActiveSubscriptions(
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
