### Title
Keeper-Controlled `tx.gasprice` Allows Draining Subscription Balance in `_processFeesAndPayKeeper` - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract reimburses keepers for gas costs using `tx.gasprice` without any upper-bound cap. Because `tx.gasprice` is fully controlled by the transaction sender (the keeper), a malicious keeper can set an arbitrarily high gas price to drain a subscription's entire ETH balance in a single `updatePriceFeeds` call.

---

### Finding Description

In `Scheduler._processFeesAndPayKeeper`, the keeper reimbursement is calculated as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
``` [1](#0-0) 

`tx.gasprice` is the effective gas price of the transaction, which is entirely set by the caller. There is no `MAX_GAS_PRICE` constant or any upper-bound check on `tx.gasprice` anywhere in the contract or its constants. [2](#0-1) 

The only guard present is a balance sufficiency check:

```solidity
if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [3](#0-2) 

This check prevents over-withdrawal but does **not** prevent the keeper from setting `tx.gasprice` high enough to make `totalKeeperFee` equal to the entire subscription balance. The contract then transfers the full balance to `msg.sender`. [4](#0-3) 

`updatePriceFeeds` has no access control — any address can call it: [5](#0-4) 

---

### Impact Explanation

A malicious keeper can drain the entire ETH balance of any active subscription in a single transaction. The subscription owner loses all deposited funds. The attacker profits by the full subscription balance minus the actual gas cost at the real network gas price. For subscriptions with large balances (e.g., permanent or well-funded subscriptions), this represents a direct, complete loss of user funds.

---

### Likelihood Explanation

- `updatePriceFeeds` is permissionless — any EOA or contract can call it.
- Valid Pyth price update data is publicly available from the Pyth price service.
- The attacker only needs to wait for the subscription's update condition (heartbeat interval or price deviation threshold) to be satisfied, which is a routine occurrence.
- Setting a high `maxPriorityFeePerGas` (EIP-1559) or `gasPrice` (legacy) is trivial and costs the attacker nothing extra beyond the actual base fee.
- No special privileges, leaked keys, or governance access are required.

---

### Recommendation

Introduce a maximum gas price cap in the keeper fee calculation. For example:

```solidity
uint256 effectiveGasPrice = tx.gasprice < MAX_KEEPER_GAS_PRICE
    ? tx.gasprice
    : MAX_KEEPER_GAS_PRICE;
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
```

`MAX_KEEPER_GAS_PRICE` should be set to a governance-configurable value reflecting a reasonable upper bound for the target chain (e.g., 500 gwei on Ethereum mainnet). Alternatively, the contract can cap `totalKeeperFee` at a fixed maximum per update, independent of `tx.gasprice`.

---

### Proof of Concept

1. Alice creates a subscription with 10 ETH balance and a 60-second heartbeat.
2. Bob (attacker) waits 60 seconds for the heartbeat condition to be satisfied.
3. Bob fetches valid Pyth price update data from the public Pyth price service.
4. Bob submits `updatePriceFeeds(subscriptionId, updateData)` with `maxPriorityFeePerGas = 10,000 ETH` (or any value large enough that `gasUsed * tx.gasprice ≥ subscriptionBalance`).
5. `_processFeesAndPayKeeper` computes `gasCost = gasUsed * 10,000 ETH ≈ 10 ETH` (for ~100k gas used).
6. The balance check passes (`10 ETH ≤ 10 ETH`).
7. The contract transfers 10 ETH to Bob.
8. Alice's subscription balance is now 0, and the subscription is effectively bricked.

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
