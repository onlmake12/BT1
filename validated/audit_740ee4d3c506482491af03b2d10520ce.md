### Title
Admin Transfer Cancellation Can Be Front-Run in `EntropyGovernance` and `SchedulerGovernance` — (`File: target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol`, `target_chains/ethereum/contracts/contracts/pulse/SchedulerGovernance.sol`)

---

### Summary

Both `EntropyGovernance` and `SchedulerGovernance` implement a two-step admin transfer via `proposeAdmin` / `acceptAdmin`. There is no explicit cancel function; the only way to revoke a pending admin proposal is to call `proposeAdmin` again with a different non-zero address. A `proposedAdmin` who is monitoring the mempool can front-run this replacement transaction by calling `acceptAdmin()` with a higher gas price, completing the transfer before the cancellation is mined.

---

### Finding Description

`EntropyGovernance.proposeAdmin` sets `_state.proposedAdmin` to any non-zero address and can be called by either the current admin or the owner: [1](#0-0) 

`acceptAdmin` allows the stored `proposedAdmin` to claim the admin role at any time: [2](#0-1) 

There is no `cancelAdmin` or equivalent function. The NatSpec comment on `proposeAdmin` says "Replaces the proposed admin if there is one," meaning the only revocation path is to call `proposeAdmin(trustedAddress)`. However, `newAdmin != address(0)` is enforced: [3](#0-2) 

This means the owner/admin cannot atomically clear `proposedAdmin` to zero — they must propose a replacement. The window between broadcasting `proposeAdmin(replacement)` and its inclusion in a block is exploitable.

`SchedulerGovernance` has an identical structure: [4](#0-3) 

The `EntropyState` confirms `proposedAdmin` is a plain storage slot with no time-lock or nonce: [5](#0-4) 

---

### Impact Explanation

A successfully front-running `proposedAdmin` becomes the admin of the Entropy contract. The admin role controls:

- `setPythFee` — can set protocol fees to any value, disrupting user economics
- `setDefaultProvider` — can redirect new users to a malicious randomness provider
- `withdrawFee` — can drain all accrued Pyth protocol fees to an arbitrary address
- `proposeAdmin` — can further transfer admin to another attacker-controlled address [6](#0-5) 

For `SchedulerGovernance`, the admin controls keeper fee settings and minimum balance per feed, which can be weaponized to grief or drain subscription balances. [7](#0-6) 

---

### Likelihood Explanation

The scenario requires:
1. The owner or admin has already called `proposeAdmin(attackerAddress)`.
2. The attacker (proposed admin) is monitoring the mempool for a replacement `proposeAdmin` call.
3. The attacker submits `acceptAdmin()` with a higher gas tip before the replacement is mined.

All three conditions are realistic on EVM chains with public mempools (Ethereum mainnet, Arbitrum, BNB Chain, etc.). The attacker has a strong financial incentive (control over fee withdrawal). MEV bots routinely monitor for exactly this pattern.

---

### Recommendation

Add an explicit zero-address cancel path, or allow `proposeAdmin(address(0))` to clear the pending proposal without proposing a new one:

```solidity
function cancelAdminProposal() external {
    _authoriseAdminAction();
    _state.proposedAdmin = address(0);
}
```

Alternatively, add a time-lock so `acceptAdmin()` can only be called after a delay, giving the owner time to cancel in the same block or a subsequent one before acceptance is possible.

---

### Proof of Concept

1. Owner calls `proposeAdmin(attackerEOA)` on the deployed Entropy contract.
2. Owner later decides `attackerEOA` is untrusted and broadcasts `proposeAdmin(trustedEOA)`.
3. `attackerEOA` observes the pending `proposeAdmin(trustedEOA)` in the mempool.
4. `attackerEOA` submits `acceptAdmin()` with `maxPriorityFeePerGas` higher than the owner's transaction.
5. `acceptAdmin()` is mined first: `_state.admin = attackerEOA`, `_state.proposedAdmin = address(0)`.
6. The owner's `proposeAdmin(trustedEOA)` is mined next but now `attackerEOA` is already the admin.
7. `attackerEOA` calls `withdrawFee(attackerEOA, accruedPythFeesInWei)` to drain all protocol fees. [2](#0-1) [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L33-40)
```text
    function proposeAdmin(address newAdmin) public virtual {
        require(newAdmin != address(0), "newAdmin is zero address");

        _authoriseAdminAction();

        _state.proposedAdmin = newAdmin;
        emit NewAdminProposed(_state.admin, newAdmin);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L45-54)
```text
    function acceptAdmin() external {
        if (msg.sender != _state.proposedAdmin)
            revert EntropyErrors.Unauthorized();

        address oldAdmin = _state.admin;
        _state.admin = msg.sender;

        _state.proposedAdmin = address(0);
        emit NewAdminAccepted(oldAdmin, msg.sender);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L67-116)
```text
    function setPythFee(uint128 newPythFee) external {
        _authoriseAdminAction();

        uint oldPythFee = _state.pythFeeInWei;
        _state.pythFeeInWei = newPythFee;

        emit PythFeeSet(oldPythFee, newPythFee);
    }

    /**
     * @dev Set the default provider of the contract
     *
     * Calls {_authoriseAdminAction}.
     *
     * Emits an {DefaultProviderSet} event.
     */
    function setDefaultProvider(address newDefaultProvider) external {
        require(
            newDefaultProvider != address(0),
            "newDefaultProvider is zero address"
        );
        _authoriseAdminAction();

        address oldDefaultProvider = _state.defaultProvider;
        _state.defaultProvider = newDefaultProvider;

        emit DefaultProviderSet(oldDefaultProvider, newDefaultProvider);
    }

    /**
     * @dev Withdraw accumulated Pyth fees to a target address
     *
     * Calls {_authoriseAdminAction}.
     *
     * Emits a {FeeWithdrawn} event.
     */
    function withdrawFee(address targetAddress, uint128 amount) external {
        require(targetAddress != address(0), "targetAddress is zero address");
        _authoriseAdminAction();

        if (amount > _state.accruedPythFeesInWei)
            revert EntropyErrors.InsufficientFee();

        _state.accruedPythFeesInWei -= amount;

        (bool success, ) = targetAddress.call{value: amount}("");
        require(success, "Failed to withdraw fees");

        emit FeeWithdrawn(targetAddress, amount);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerGovernance.sol (L34-54)
```text
    function proposeAdmin(address newAdmin) public virtual {
        require(newAdmin != address(0), "newAdmin is zero address");

        _authorizeAdminAction();

        _state.proposedAdmin = newAdmin;
        emit NewAdminProposed(_state.admin, newAdmin);
    }

    /**
     * @dev The proposed admin accepts the admin transfer.
     */
    function acceptAdmin() external {
        if (msg.sender != _state.proposedAdmin)
            revert SchedulerErrors.Unauthorized();

        address oldAdmin = _state.admin;
        _state.admin = msg.sender;

        _state.proposedAdmin = address(0);
        emit NewAdminAccepted(oldAdmin, msg.sender);
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerGovernance.sol (L68-89)
```text
    function setSingleUpdateKeeperFeeInWei(uint128 newFee) external {
        _authorizeAdminAction();

        uint oldFee = _state.singleUpdateKeeperFeeInWei;
        _state.singleUpdateKeeperFeeInWei = newFee;

        emit SingleUpdateKeeperFeeSet(oldFee, newFee);
    }

    /**
     * @dev Set the minimum balance required per feed in a subscription.
     * Calls {_authorizeAdminAction}.
     * Emits a {MinimumBalancePerFeedSet} event.
     */
    function setMinimumBalancePerFeed(uint128 newMinimumBalance) external {
        _authorizeAdminAction();

        uint oldBalance = _state.minimumBalancePerFeed;
        _state.minimumBalancePerFeed = newMinimumBalance;

        emit MinimumBalancePerFeedSet(oldBalance, newMinimumBalance);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L37-39)
```text
        // proposedAdmin is the new admin's account address proposed by either the owner or the current admin.
        // If there is no pending transfer request, this value will hold `address(0)`.
        address proposedAdmin;
```
