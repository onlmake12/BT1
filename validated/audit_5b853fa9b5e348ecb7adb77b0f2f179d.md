### Title
Uncapped `tx.gasprice` in Keeper Fee Calculation Allows Subscription Balance Drain - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `_processFeesAndPayKeeper` function in `Scheduler.sol` computes the keeper's gas reimbursement as `(gasUsed + GAS_OVERHEAD) * tx.gasprice` with no upper bound on `tx.gasprice`. Because `updatePriceFeeds` has no access control, any unprivileged caller acting as a keeper can submit the transaction with an arbitrarily inflated gas price, extracting a profit of at least `GAS_OVERHEAD * tx.gasprice` from the subscription's balance on every valid update.

---

### Finding Description

`_processFeesAndPayKeeper` calculates the total keeper fee as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
``` [1](#0-0) 

`tx.gasprice` is entirely attacker-controlled. There is no cap, no comparison against a reference gas price, and no governance-set maximum. `GAS_OVERHEAD` is a fixed constant of 30,000 gas units: [2](#0-1) 

`updatePriceFeeds` carries no access-control modifier — any address may call it: [3](#0-2) 

The keeper fee is then transferred directly to `msg.sender`: [4](#0-3) 

---

### Impact Explanation

An attacker who submits `updatePriceFeeds` with `tx.gasprice = P` pays `P * actualGasUsed` in network fees but receives `P * (actualGasUsed + GAS_OVERHEAD) + keeperSpecificFee` from the subscription balance. The guaranteed net profit per call is:

```
profit = GAS_OVERHEAD * P + keeperSpecificFee
       = 30,000 * P + keeperSpecificFee
```

At `P = 10,000 gwei` (achievable on congested L1 or by setting a large EIP-1559 priority fee), the attacker extracts `0.3 ETH` per call purely from the `GAS_OVERHEAD` component, independent of actual gas consumption. A subscription with a large balance can be drained to zero in a single transaction if `P` is set high enough to make `totalKeeperFee >= status.balanceInWei`. Legitimate subscribers lose their entire deposited balance with no recourse.

---

### Likelihood Explanation

- `updatePriceFeeds` is permissionless; no whitelist, role, or stake is required.
- The attacker only needs to supply valid Pyth update data satisfying the subscription's update criteria (heartbeat elapsed or deviation threshold crossed), which is publicly available on-chain.
- The attack is profitable at any gas price above the network base fee because the `GAS_OVERHEAD` surplus is always reimbursed from the subscription, not from the attacker.
- The attack is repeatable every time the update criteria are met (e.g., every heartbeat interval).

---

### Recommendation

1. **Cap `tx.gasprice`**: Introduce a governance-settable `maxGasPriceInWei` parameter and clamp the reimbursement:
   ```solidity
   uint256 effectiveGasPrice = Math.min(tx.gasprice, _state.maxGasPriceInWei);
   uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
   ```
2. **Use a TWAP or oracle gas price**: Reimburse based on a time-weighted average gas price rather than the instantaneous `tx.gasprice`.
3. **Restrict keeper role**: Require keepers to be whitelisted or bonded so that malicious behaviour can be penalised.

---

### Proof of Concept

1. Attacker deploys a contract or uses an EOA.
2. A subscription exists with `updateOnHeartbeat = true` and `heartbeatSeconds = 3600`. The heartbeat interval has elapsed.
3. Attacker calls:
   ```solidity
   scheduler.updatePriceFeeds{gasPrice: 100_000 gwei}(subscriptionId, validUpdateData);
   ```
4. Inside `_processFeesAndPayKeeper`:
   - `gasCost = (actualGasUsed + 30_000) * 100_000 gwei`
   - If `actualGasUsed ≈ 200_000`, `gasCost ≈ (230_000) * 100_000 gwei = 23 ETH`
   - Attacker paid `200_000 * 100_000 gwei = 20 ETH` in gas.
   - Net profit: `3 ETH` (the `GAS_OVERHEAD` surplus) plus `keeperSpecificFee`.
5. The subscription's `balanceInWei` is reduced by `23 ETH + keeperSpecificFee`; the attacker receives that amount minus the Pyth fee already deducted. [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-278)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L29-29)
```text
    uint256 public constant GAS_OVERHEAD = 30000;
```
