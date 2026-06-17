### Title
Hardcoded `GAS_OVERHEAD` Constant Causes Inaccurate Keeper Fee Accounting - (File: `target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol`)

---

### Summary

The `GAS_OVERHEAD` constant in `SchedulerConstants.sol` is hardcoded to `30000` gas units and is used directly in `Scheduler._processFeesAndPayKeeper` to compute keeper reimbursements. Because this value is a Solidity `constant` (compile-time immutable), it cannot be updated without a full contract upgrade. If EVM gas costs change due to a fork, or if the Scheduler is deployed on a different EVM-compatible chain with different base transaction costs, the keeper fee accounting will be systematically wrong.

---

### Finding Description

`SchedulerConstants.sol` defines:

```solidity
/// Fixed gas overhead component used in keeper fee calculation.
/// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
uint256 public constant GAS_OVERHEAD = 30000;
``` [1](#0-0) 

This constant is consumed in `Scheduler._processFeesAndPayKeeper`:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
``` [2](#0-1) 

The `GAS_OVERHEAD` is intended to cover the base transaction cost (21,000 gas) plus any EVM overhead not captured between the `gasleft()` snapshot at the top of `updatePriceFeeds` and the `gasleft()` call inside `_processFeesAndPayKeeper`. The entry point is the permissionless `updatePriceFeeds` function, which any keeper (unprivileged transaction sender) can call:

```solidity
function updatePriceFeeds(
    uint256 subscriptionId,
    bytes[] calldata updateData
) external override {
    uint256 startGas = gasleft();
    ...
    _processFeesAndPayKeeper(status, startGas, params.priceIds.length);
``` [3](#0-2) 

Because `GAS_OVERHEAD` is a `constant`, it is baked into the bytecode at compile time. There is no setter, no governance function, and no initializer parameter to adjust it. The Scheduler is explicitly designed for multi-chain deployment (Ethereum mainnet, L2s, and other EVM-compatible chains), where base transaction costs and opcode pricing differ significantly from Ethereum mainnet assumptions.

---

### Impact Explanation

**Under-compensation scenario (GAS_OVERHEAD too low):** If the actual transaction overhead exceeds 30,000 gas (e.g., on a chain where calldata or base transaction costs are higher), keepers are systematically under-reimbursed. Over time this makes keeper operation unprofitable, potentially causing the Scheduler service to stall — price feeds stop being updated for all active subscriptions.

**Over-compensation scenario (GAS_OVERHEAD too high):** If the actual overhead is lower than 30,000 gas (e.g., on an L2 with cheaper base costs), subscription owners are overcharged on every update. Their `balanceInWei` is drained faster than the true cost, shortening subscription lifetimes and causing premature `InsufficientBalance` reverts.

Both scenarios affect real funds: `status.balanceInWei` is decremented and ETH is transferred to the keeper on every call. [4](#0-3) 

---

### Likelihood Explanation

**Medium.** The Pyth Scheduler is explicitly intended for multi-chain deployment. EVM-compatible chains (Arbitrum, Optimism, Base, BNB Chain, etc.) have materially different gas cost structures from Ethereum mainnet. Additionally, EVM forks have historically changed base transaction costs and opcode pricing (e.g., EIP-2929, EIP-3529). The 30,000 figure is acknowledged in the code itself as only "a rough estimate." [1](#0-0) 

---

### Recommendation

1. Move `GAS_OVERHEAD` from a compile-time `constant` to a storage variable initialized in `_initialize` and protected by an admin-only setter function, analogous to how `singleUpdateKeeperFeeInWei` is already handled.
2. Emit an event when `GAS_OVERHEAD` is updated so off-chain monitoring can track changes.
3. Document the per-chain calibrated values in deployment scripts. [5](#0-4) 

---

### Proof of Concept

1. Deploy `Scheduler` on an EVM-compatible chain where the base transaction cost differs from Ethereum mainnet (e.g., a chain where the 21,000 base cost has been modified by a fork, or an L2 where calldata pricing dominates).
2. An unprivileged keeper calls `updatePriceFeeds(subscriptionId, updateData)`.
3. Inside `_processFeesAndPayKeeper`, the fee is computed as:
   ```
   gasCost = (startGas - gasleft() + 30000) * tx.gasprice
   ```
4. The actual transaction overhead on this chain is, say, 50,000 gas. The keeper receives reimbursement for only `actual_execution_gas + 30,000` instead of `actual_execution_gas + 50,000`, a shortfall of `20,000 * tx.gasprice` per call.
5. Repeated calls drain the keeper's profitability margin. Alternatively, if the chain overhead is 10,000 gas, the subscription owner is overcharged by `20,000 * tx.gasprice` per call, prematurely exhausting `balanceInWei`. [6](#0-5)

### Citations

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L27-29)
```text
    /// Fixed gas overhead component used in keeper fee calculation.
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L16-30)
```text
    function _initialize(
        address admin,
        address pythAddress,
        uint128 minimumBalancePerFeed,
        uint128 singleUpdateKeeperFeeInWei
    ) internal {
        require(admin != address(0), "admin is zero address");
        require(pythAddress != address(0), "pyth is zero address");

        _state.pyth = pythAddress;
        _state.admin = admin;
        _state.subscriptionNumber = 1;
        _state.minimumBalancePerFeed = minimumBalancePerFeed;
        _state.singleUpdateKeeperFeeInWei = singleUpdateKeeperFeeInWei;
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
