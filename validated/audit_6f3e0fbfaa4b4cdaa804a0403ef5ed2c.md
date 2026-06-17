### Title
`withdraw()` and `withdrawAsFeeManager()` Lack Recipient Parameter, Permanently Locking Accrued Fees When Caller Cannot Receive ETH - (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol` and `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

Both `Entropy.sol::withdraw()`, `Entropy.sol::withdrawAsFeeManager()`, and `Echo.sol::withdrawAsFeeManager()` hardcode the ETH recipient to `msg.sender` with no way to specify an alternative address. If the provider or fee manager is a smart contract that cannot receive ETH (no `receive()`/`fallback()` function, or one that reverts on ETH receipt), all accrued fees are permanently locked in the contract.

---

### Finding Description

`Entropy.sol::withdraw()` transfers accrued fees exclusively to `msg.sender`:

```solidity
(bool sent, ) = msg.sender.call{value: amount}("");
require(sent, "withdrawal to msg.sender failed");
```

`Entropy.sol::withdrawAsFeeManager()` similarly sends to `msg.sender` (the fee manager):

```solidity
(bool sent, ) = msg.sender.call{value: amount}("");
require(sent, "withdrawal to msg.sender failed");
```

`Echo.sol::withdrawAsFeeManager()` has the identical pattern:

```solidity
(bool sent, ) = msg.sender.call{value: amount}("");
require(sent, "Failed to send fees");
```

`Echo.sol::withdrawFees()` (admin-only Pyth protocol fee withdrawal) also sends to `msg.sender`:

```solidity
(bool sent, ) = msg.sender.call{value: amount}("");
require(sent, "Failed to send fees");
```

None of these functions accept a `recipient` parameter. The only escape valve — `EntropyGovernance::withdrawFee(address targetAddress, uint128 amount)` — applies only to Pyth-protocol-accrued fees, not provider fees.

The fee manager role is explicitly designed to be a separate contract address. The Fortuna keeper system itself uses `withdrawAsFeeManager` with a contract-based fee manager. A multisig, DAO treasury, or automated keeper contract set as fee manager that lacks a `receive()` function will cause every `withdrawAsFeeManager` call to revert, permanently locking the provider's entire accrued fee balance.

---

### Impact Explanation

A provider's accrued fees in `providerInfo.accruedFeesInWei` become permanently unrecoverable. There is no admin override, no alternative withdrawal path, and no way to change the destination of the ETH transfer without changing `msg.sender` itself. The funds are locked in the Entropy/Echo contract indefinitely.

---

### Likelihood Explanation

Smart contract fee managers are a primary use case: the Fortuna keeper infrastructure explicitly uses `withdrawAsFeeManager` with a contract-based fee manager wallet. Multisigs (Gnosis Safe), DAO treasuries, and automated keeper contracts are common choices for fee manager roles. Many such contracts do not implement `receive()` or implement it with logic that can revert (e.g., access-controlled receive functions). This is a realistic, non-exotic scenario.

---

### Recommendation

Add a `recipient` parameter to `withdraw()`, `withdrawAsFeeManager()`, and `withdrawFees()`:

```solidity
function withdraw(uint128 amount, address recipient) public override {
    // ...
    (bool sent, ) = recipient.call{value: amount}("");
    require(sent, "withdrawal failed");
}

function withdrawAsFeeManager(
    address provider,
    uint128 amount,
    address recipient
) external override {
    // ...
    (bool sent, ) = recipient.call{value: amount}("");
    require(sent, "withdrawal failed");
}
```

---

### Proof of Concept

1. Provider `P` (an EOA) registers with Entropy and sets a Gnosis Safe multisig `M` (which has no `receive()` function) as fee manager via `setFeeManager(M)`.
2. Users make entropy requests; `providerInfo.accruedFeesInWei` accumulates for `P`.
3. `M` calls `withdrawAsFeeManager(P, amount)`.
4. The contract executes `(bool sent, ) = msg.sender.call{value: amount}("")` — `msg.sender` is `M`.
5. The Gnosis Safe has no `receive()` function → `sent == false`.
6. `require(sent, "withdrawal to msg.sender failed")` reverts.
7. The state rollback restores `providerInfo.accruedFeesInWei` (CEI pattern), but the call will revert identically on every future attempt.
8. `P` cannot change `msg.sender`; there is no admin path to redirect provider fees. All of `P`'s accrued fees are permanently locked. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L150-173)
```text
    function withdraw(uint128 amount) public override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            msg.sender
        ];

        // Use checks-effects-interactions pattern to prevent reentrancy attacks.
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");

        emit EntropyEvents.Withdrawal(msg.sender, msg.sender, amount);
        emit EntropyEventsV2.Withdrawal(
            msg.sender,
            msg.sender,
            amount,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L175-209)
```text
    function withdrawAsFeeManager(
        address provider,
        uint128 amount
    ) external override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];

        if (providerInfo.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }

        if (providerInfo.feeManager != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }

        // Use checks-effects-interactions pattern to prevent reentrancy attacks.
        require(
            providerInfo.accruedFeesInWei >= amount,
            "Insufficient balance"
        );
        providerInfo.accruedFeesInWei -= amount;

        // Interaction with an external contract or token transfer
        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "withdrawal to msg.sender failed");

        emit EntropyEvents.Withdrawal(provider, msg.sender, amount);
        emit EntropyEventsV2.Withdrawal(
            provider,
            msg.sender,
            amount,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L289-299)
```text
    function withdrawFees(uint128 amount) external override {
        require(msg.sender == _state.admin, "Only admin can withdraw fees");
        require(_state.accruedFeesInWei >= amount, "Insufficient balance");

        _state.accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L360-379)
```text
    function withdrawAsFeeManager(
        address provider,
        uint128 amount
    ) external override {
        require(
            msg.sender == _state.providers[provider].feeManager,
            "Only fee manager"
        );
        require(
            _state.providers[provider].accruedFeesInWei >= amount,
            "Insufficient balance"
        );

        _state.providers[provider].accruedFeesInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send fees");

        emit FeesWithdrawn(msg.sender, amount);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L103-116)
```text
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
