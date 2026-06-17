### Title
Hardcoded `msg.sender` Recipient in `withdraw()` and `withdrawAsFeeManager()` Locks Provider Fees When Caller Is a Non-Payable Contract - (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.withdraw()` and `Entropy.withdrawAsFeeManager()` both hardcode the ETH recipient to `msg.sender`. If the provider or fee manager is a smart contract (e.g., a multisig) that lacks a `receive()` function or whose fallback consumes more than the 2300 gas stipend, the ETH transfer will revert and the provider's accrued fees will be permanently locked in the contract. The same pattern exists in `Echo.sol`'s `withdrawFees()` and `withdrawAsFeeManager()`.

---

### Finding Description

In `Entropy.sol`, the `withdraw()` function deducts `amount` from the provider's `accruedFeesInWei` and then sends ETH unconditionally to `msg.sender`:

```solidity
function withdraw(uint128 amount) public override {
    EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[msg.sender];
    require(providerInfo.accruedFeesInWei >= amount, "Insufficient balance");
    providerInfo.accruedFeesInWei -= amount;

    (bool sent, ) = msg.sender.call{value: amount}("");
    require(sent, "withdrawal to msg.sender failed");   // <-- hardcoded recipient
    ...
}
``` [1](#0-0) 

The same pattern appears in `withdrawAsFeeManager()`, where the fee manager's accrued fees are sent to `msg.sender` (the fee manager address):

```solidity
(bool sent, ) = msg.sender.call{value: amount}("");
require(sent, "withdrawal to msg.sender failed");
``` [2](#0-1) 

Neither function accepts a `recipient` parameter. There is no alternative withdrawal path that allows the caller to redirect funds to a different address.

The identical pattern exists in `Echo.sol`:

- `withdrawFees()` sends to `msg.sender` (admin)
- `withdrawAsFeeManager()` sends to `msg.sender` (fee manager) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

If a provider or fee manager is a smart contract (e.g., a Gnosis Safe multisig, a DAO treasury, or any contract without a `receive()` / `fallback()` function), the low-level `.call{value: amount}("")` will fail. Because the state update (`accruedFeesInWei -= amount`) happens before the transfer (checks-effects-interactions), the revert on the `require(sent, ...)` line will roll back the entire transaction. The provider's fees remain in the contract but the provider has no alternative path to withdraw them — the only withdrawal function always sends to `msg.sender`.

**Result**: Accrued provider fees are permanently locked in the Entropy contract. There is no admin override or alternative withdrawal path.

**Impact: Medium** — funds are locked, not stolen. The amount at risk scales with the provider's accrued fees.

---

### Likelihood Explanation

Entropy providers are expected to be infrastructure operators. It is common practice for such operators to use multisig wallets (Gnosis Safe) or DAO-controlled contracts as their registered provider address for security reasons. Gnosis Safe contracts do have a `receive()` function, but other contract types (proxy contracts, custom treasury contracts, contracts with gas-heavy fallbacks) may not. The likelihood is **Medium**.

---

### Recommendation

Add an optional `recipient` parameter to `withdraw()` and `withdrawAsFeeManager()`, defaulting to `msg.sender` if not specified, or provide an overloaded version:

```solidity
function withdraw(uint128 amount, address payable recipient) public {
    EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[msg.sender];
    require(providerInfo.accruedFeesInWei >= amount, "Insufficient balance");
    providerInfo.accruedFeesInWei -= amount;
    (bool sent, ) = recipient.call{value: amount}("");
    require(sent, "withdrawal failed");
    emit EntropyEventsV2.Withdrawal(msg.sender, recipient, amount, bytes(""));
}
```

Apply the same fix to `withdrawAsFeeManager()` in both `Entropy.sol` and `Echo.sol`.

---

### Proof of Concept

1. Deploy a contract `ProviderContract` that registers as an Entropy provider but has no `receive()` function.
2. Users make entropy requests, accruing fees to `ProviderContract.address`.
3. `ProviderContract` calls `entropy.withdraw(accruedAmount)`.
4. The `msg.sender.call{value: amount}("")` call fails because `ProviderContract` has no `receive()`.
5. The `require(sent, "withdrawal to msg.sender failed")` reverts the transaction.
6. `accruedFeesInWei` is restored (due to revert), but the provider has no other way to withdraw — fees are permanently inaccessible. [5](#0-4) [6](#0-5)

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
