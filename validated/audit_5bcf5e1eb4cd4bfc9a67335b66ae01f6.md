### Title
Uncapped `tx.gasprice` in Keeper Fee Calculation Allows Malicious Keeper to Drain Subscription Balance in a Single Transaction - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract's `_processFeesAndPayKeeper` function computes the keeper reimbursement using `tx.gasprice` directly, with no upper bound. Because `updatePriceFeeds` is permissionless, any caller can set an arbitrarily high gas price to inflate the computed `gasCost`, draining the entire subscription balance in a single update call instead of the many updates the subscription manager funded for.

---

### Finding Description

`Scheduler.updatePriceFeeds` is callable by any address (permissionless keeper). At the end of a successful update it calls `_processFeesAndPayKeeper`:

```solidity
// Scheduler.sol  _processFeesAndPayKeeper
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;

if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}

status.balanceInWei -= totalKeeperFee;
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
``` [1](#0-0) 

`tx.gasprice` is entirely attacker-controlled. There is no cap, no sanity check, and no comparison against an oracle or a protocol-configured maximum. The only guard is the `InsufficientBalance` revert, which merely prevents the keeper from taking *more* than the current balance — it does not prevent the keeper from taking *all* of it in one shot.

`GAS_OVERHEAD` is a fixed constant of 30,000 gas units: [2](#0-1) 

The `updatePriceFeeds` entry point is fully permissionless: [3](#0-2) 

---

### Impact Explanation

A subscription manager deposits funds expecting them to cover many price-update cycles at prevailing gas prices. A malicious keeper can drain the entire balance in a single transaction:

- Let `B` = subscription balance (e.g., 1 ETH), `G` = actual gas used (≈ 200 000 units).
- Attacker sets `tx.gasprice = P` such that `(G + 30 000) * P ≥ B`.
- For `B = 1 ETH`, `G = 200 000`: `P ≥ 1 ETH / 230 000 ≈ 4 348 gwei`.
- The attacker pays `G * P ≈ 200 000 * 4 348 gwei ≈ 0.87 ETH` in transaction cost.
- The attacker receives `B = 1 ETH` from the subscription.
- **Net profit ≈ 0.13 ETH** (the `GAS_OVERHEAD` component + `keeperSpecificFee`).

The subscription manager loses their entire deposited balance after only one update instead of the hundreds or thousands of updates they funded for. This is a direct, quantifiable loss of user funds.

---

### Likelihood Explanation

- `updatePriceFeeds` requires no registration, no stake, and no privileged role — any EOA can call it.
- The attacker only needs to supply valid Wormhole-signed Pyth update data (freely available from Hermes) that satisfies the subscription's trigger criteria.
- The attack is profitable whenever `GAS_OVERHEAD * tx.gasprice + keeperSpecificFee > attacker's transaction cost overhead`, which holds at any gas price above a trivially low threshold.
- The attack is atomic and irreversible in a single transaction.

---

### Recommendation

Introduce a configurable maximum gas price cap enforced inside `_processFeesAndPayKeeper`:

```solidity
uint256 effectiveGasPrice = tx.gasprice;
if (_state.maxKeeperGasPriceInWei > 0) {
    effectiveGasPrice = Math.min(tx.gasprice, _state.maxKeeperGasPriceInWei);
}
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
```

Alternatively, compute the reimbursable gas price as `min(tx.gasprice, block.basefee * MULTIPLIER)` to anchor it to the network's actual base fee, preventing artificial inflation while still covering legitimate high-gas-price environments.

---

### Proof of Concept

1. A subscription manager creates a subscription with 1 ETH balance, heartbeat = 60 s, 2 price feeds.
2. Attacker waits 60 s for the heartbeat to trigger.
3. Attacker fetches valid Pyth update data from Hermes for the two subscribed price feeds.
4. Attacker calls `scheduler.updatePriceFeeds(subscriptionId, updateData)` with `tx.gasprice` set to `≥ 1 ETH / (gasUsed + 30 000)` (e.g., 5 000 gwei).
5. `_processFeesAndPayKeeper` computes `totalKeeperFee ≥ 1 ETH`, passes the `balanceInWei` check (since `balanceInWei == 1 ETH`), deducts the full balance, and transfers it to `msg.sender`.
6. The subscription balance is now 0. The subscription manager's 1 ETH is gone after a single update. [4](#0-3)

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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L28-29)
```text
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
```
