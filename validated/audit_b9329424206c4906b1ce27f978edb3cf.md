### Title
Keeper Fee Distribution in `updatePriceFeeds` Is Susceptible to MEV Front-Running — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `updatePriceFeeds` function in `Scheduler.sol` pays keeper fees to `msg.sender` with no front-running protection. Because the Pyth `updateData` payload is publicly obtainable from the Hermes API, any MEV bot can copy a pending keeper transaction from the mempool, resubmit it with a higher gas price, and capture the full keeper fee. The original keeper's transaction then reverts, and they lose their gas cost with no compensation.

---

### Finding Description

`updatePriceFeeds` is a permissionless function — any address may call it with valid `updateData`: [1](#0-0) 

At the end of a successful call, `_processFeesAndPayKeeper` computes and transfers the keeper reward directly to `msg.sender`: [2](#0-1) 

The reward has two components:

1. **Gas reimbursement**: `(startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice` — the caller is made whole for their gas spend at whatever `tx.gasprice` they used.
2. **Fixed per-feed incentive**: `singleUpdateKeeperFeeInWei * numPriceIds` — a protocol-set bonus paid on top of gas. [3](#0-2) 

`GAS_OVERHEAD` is a fixed constant of 30 000 gas: [4](#0-3) 

`singleUpdateKeeperFeeInWei` is a governance-controlled value stored in state: [5](#0-4) 

Because the gas reimbursement scales with `tx.gasprice`, a front-runner who submits the identical `updateData` at a higher gas price is fully reimbursed for the premium they paid. Their **net profit is always `singleUpdateKeeperFeeInWei × numPriceIds`**, independent of gas price. The original keeper's transaction then hits: [6](#0-5) 

…and reverts with `TimestampOlderThanLastUpdate`, forfeiting the original keeper's gas cost entirely.

---

### Impact Explanation

- **Legitimate keepers lose their gas cost** on every front-run attempt (transaction reverts, no reimbursement).
- **The attacker nets `singleUpdateKeeperFeeInWei × numPriceIds`** on every stolen update, regardless of the gas premium paid.
- Sustained front-running makes honest keeper operation economically irrational, degrading the liveness of the Scheduler's price-feed update mechanism and potentially causing subscriptions to go stale.

---

### Likelihood Explanation

**High.** The `updateData` payload is not secret — it is freely obtainable from the Pyth Hermes API at any time. An attacker does not even need to observe the mempool; they can race independently. On chains with a public mempool (Ethereum mainnet, most L2s), the attack is trivially automated: watch for `updatePriceFeeds` calldata, rebroadcast with `+1 gwei`, profit. The attack is always profitable as long as `singleUpdateKeeperFeeInWei × numPriceIds` exceeds the marginal cost of the gas-price bump, which is negligible.

---

### Recommendation

1. **Commit-reveal for keepers**: require keepers to commit to a `keccak256(updateData, keeperAddress)` one block ahead; only the committing address may reveal and collect the fee.
2. **Keeper whitelist / round-robin**: restrict `updatePriceFeeds` to a set of registered keepers, eliminating open competition.
3. **Flat fee only (remove gas reimbursement)**: if gas reimbursement is removed, a front-runner must pay the full gas premium out of pocket, making the attack unprofitable when the premium exceeds `singleUpdateKeeperFeeInWei × numPriceIds`.
4. **EIP-712 signed update bundles**: bind the `updateData` to a specific `msg.sender` off-chain before submission, so the payload is non-transferable.

---

### Proof of Concept

```
1. Keeper A obtains fresh updateData from Hermes for subscription S (N price feeds).
2. Keeper A submits updatePriceFeeds(S, updateData) at gasPrice = P.
3. MEV bot B observes the pending tx in the mempool (or races independently).
4. Bot B submits updatePriceFeeds(S, updateData) at gasPrice = P + delta.
5. Bot B is included first:
      - receives gasCost_B + singleUpdateKeeperFeeInWei * N
      - gasCost_B is fully reimbursed → net profit = singleUpdateKeeperFeeInWei * N
6. Keeper A's tx is mined next:
      - _validateShouldUpdatePrices reverts: TimestampOlderThanLastUpdate
      - Keeper A loses gas_A * P with zero compensation.
``` [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-279)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
        uint256 startGas = gasleft();
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L332-347)
```text
        uint256 latestPublishTime = _validateShouldUpdatePrices(
            subscriptionId,
            params,
            status,
            priceFeeds
        );

        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;

        _storePriceUpdates(subscriptionId, priceFeeds);

        _processFeesAndPayKeeper(status, startGas, params.priceIds.length);

        emit PricesUpdated(subscriptionId, latestPublishTime);
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L388-397)
```text
        // Reject updates if they're older than the latest stored ones
        if (
            status.priceLastUpdatedAt > 0 &&
            updateTimestamp <= status.priceLastUpdatedAt
        ) {
            revert SchedulerErrors.TimestampOlderThanLastUpdate(
                updateTimestamp,
                status.priceLastUpdatedAt
            );
        }
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

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerState.sol (L20-20)
```text
        uint128 singleUpdateKeeperFeeInWei;
```
