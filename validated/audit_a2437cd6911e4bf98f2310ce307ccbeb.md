### Title
Keeper Can Inflate `tx.gasprice` to Drain Subscription Balance in a Single Update — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `_processFeesAndPayKeeper` function in `Scheduler.sol` computes the keeper reimbursement using the raw, uncapped `tx.gasprice`. Any unprivileged keeper can set an arbitrarily high gas price to drain a subscription's entire balance in a single `updatePriceFeeds` call, extracting `GAS_OVERHEAD × tx.gasprice` as pure profit at the subscription manager's expense.

---

### Finding Description

In `_processFeesAndPayKeeper`:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;

if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
status.balanceInWei -= totalKeeperFee;
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
``` [1](#0-0) 

`tx.gasprice` is entirely keeper-controlled. There is no upper bound, no oracle-based reference price, and no TWAP guard. The keeper pays `gasUsed × tx.gasprice` to the block producer and receives `(gasUsed + GAS_OVERHEAD) × tx.gasprice + singleUpdateKeeperFeeInWei × numPriceIds` from the subscription. The keeper's net profit per call is therefore:

```
profit = GAS_OVERHEAD × tx.gasprice + singleUpdateKeeperFeeInWei × numPriceIds
```

By choosing `tx.gasprice` such that `totalKeeperFee ≈ status.balanceInWei`, the keeper drains the entire subscription balance in one transaction. The only on-chain guard is the `InsufficientBalance` revert, which the keeper avoids by calibrating the gas price precisely (feasible via `eth_call` simulation before submission).

The `updatePriceFeeds` entry point captures `startGas = gasleft()` at the very top of the call, so the entire function's gas consumption is included in the reimbursement: [2](#0-1) 

The Pyth fee is deducted from the subscription balance before the keeper fee is computed, so the keeper's inflated payment comes entirely from the subscription manager's deposited funds: [3](#0-2) 

---

### Impact Explanation

Let `B` = subscription balance after the Pyth fee deduction, `G` = actual gas used inside `updatePriceFeeds`, and `O` = `GAS_OVERHEAD`.

The keeper sets `tx.gasprice = B / (G + O)`. Then:

| Item | Value |
|---|---|
| Keeper pays to network | `G × B / (G + O)` |
| Keeper receives from subscription | `B` |
| **Net keeper profit** | **`O × B / (G + O)`** |

For representative values `O = 50 000`, `G = 200 000`: the keeper extracts **20 % of the subscription balance as profit in a single update**. The subscription manager loses their entire balance instead of receiving the expected number of price-feed updates. Larger `GAS_OVERHEAD` values increase the attacker's profit fraction.

This is a direct financial loss to subscription managers — a class of unprivileged users who deposit native tokens into the Scheduler contract.

---

### Likelihood Explanation

The attack is economically rational whenever the subscription balance is large relative to the keeper's upfront gas cost. The keeper needs only:

1. Enough ETH to pay `G × tx.gasprice` to the block producer (recoverable from the subscription payment).
2. An off-chain `eth_call` simulation to estimate `G` accurately before submission.
3. A valid Pyth price-update payload satisfying the subscription's trigger criteria (publicly available from Hermes).

No privileged access, no governance key, and no oracle manipulation is required. Any address can call `updatePriceFeeds`.

---

### Recommendation

Cap the effective gas price used in the reimbursement calculation to a governance-controlled maximum or a recent on-chain gas price oracle:

```solidity
uint256 effectiveGasPrice = Math.min(tx.gasprice, _state.maxGasPriceInWei);
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
```

Alternatively, use a block-base-fee reference (`block.basefee + maxPriorityFeePerGas`) with a reasonable cap to prevent manipulation while still reimbursing legitimate keepers fairly.

---

### Proof of Concept

```
Setup:
  subscription balance B = 1 ETH (after Pyth fee)
  GAS_OVERHEAD O = 50 000
  estimated gasUsed G = 200 000 (from eth_call simulation)

Attack:
  target_gasprice = B / (G + O)
                  = 1e18 / 250 000
                  ≈ 4 000 gwei

  keeper calls updatePriceFeeds(...) with tx.gasprice = 4 000 gwei

Result:
  keeper pays to network:  200 000 × 4 000 gwei = 0.8 ETH
  keeper receives from sub: 250 000 × 4 000 gwei = 1.0 ETH
  net profit:               0.2 ETH  (20 % of subscription balance)
  subscription balance:     0 ETH    (drained in one update)
```

The subscription manager funded the contract expecting many updates; instead, a single keeper call at an inflated gas price empties the balance entirely, analogous to the external report's front-run that consumes the entire output amount as a fee by exploiting an unguarded state variable.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L279-279)
```text
        uint256 startGas = gasleft();
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L305-306)
```text
        status.balanceInWei -= pythFee;
        status.totalSpent += pythFee;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L845-863)
```text
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
