### Title
Missing Zero-Address Validation in `withdrawFee` Governance Action Allows Permanent Burning of Protocol Fees - (File: `target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol`)

### Summary
The `withdrawFee` function in `PythGovernance.sol`, executed via a governance VAA, does not validate that `payload.targetAddress != address(0)`. A governance VAA encoding a zero target address will cause the contract to send ETH to `address(0)`, permanently burning accumulated protocol fees. The sibling contract `EntropyGovernance.sol` already applies this exact check, confirming the omission is unintentional.

### Finding Description
`PythGovernance.withdrawFee` is invoked when `executeGovernanceInstruction` processes a `GovernanceAction.WithdrawFee` VAA:

```
// PythGovernance.sol L264-272
function withdrawFee(WithdrawFeePayload memory payload) internal {
    if (payload.fee > address(this).balance)
        revert PythErrors.InsufficientFee();

    (bool success, ) = payload.targetAddress.call{value: payload.fee}("");
    require(success, "Failed to withdraw fees");

    emit FeeWithdrawn(payload.targetAddress, payload.fee);
}
```

`payload.targetAddress` is decoded directly from the VAA payload in `parseWithdrawFeePayload` with no zero-address check:

```
// PythGovernanceInstructions.sol L261
wf.targetAddress = address(encodedPayload.toAddress(index));
```

There is no guard anywhere in the call chain against `targetAddress == address(0)`.

In EVM, `address(0).call{value: amount}("")` succeeds (`success = true`) and the ETH is permanently destroyed. The function will emit `FeeWithdrawn(address(0), fee)` and return without error, silently burning the funds.

By contrast, `EntropyGovernance.withdrawFee` explicitly guards against this:

```
// EntropyGovernance.sol L103-104
function withdrawFee(address targetAddress, uint128 amount) external {
    require(targetAddress != address(0), "targetAddress is zero address");
```

The Stylus implementation (`target_chains/stylus/contracts/pyth-receiver/src/governance.rs`) has the same omission — `withdraw_fee` transfers ETH to `target_address` with no zero-address check.

### Impact Explanation
Any accumulated ETH balance held by the Pyth EVM contract (collected as update fees and transaction fees) can be permanently burned if a `WithdrawFee` governance VAA is submitted with `targetAddress = 0x0000…0000`. The ETH is irrecoverable. This constitutes a direct, permanent loss of protocol treasury funds.

### Likelihood Explanation
`executeGovernanceInstruction` is a public function callable by any governance message submitter who holds a valid Wormhole-signed VAA. A governance operator error (e.g., encoding a zero address when constructing the VAA off-chain) or a subtle bug in the VAA-construction tooling is a realistic scenario. The `WithdrawFee.ts` governance payload encoder does not enforce a non-zero address either, making an accidental zero-address submission plausible. The inconsistency with `EntropyGovernance.sol` — which already has the guard — further raises the likelihood that this path has not been hardened.

### Recommendation
Add a zero-address check in `PythGovernance.withdrawFee`:

```solidity
function withdrawFee(WithdrawFeePayload memory payload) internal {
    if (payload.targetAddress == address(0))
        revert PythErrors.InvalidGovernanceMessage(); // or a dedicated error
    if (payload.fee > address(this).balance)
        revert PythErrors.InsufficientFee();
    (bool success, ) = payload.targetAddress.call{value: payload.fee}("");
    require(success, "Failed to withdraw fees");
    emit FeeWithdrawn(payload.targetAddress, payload.fee);
}
```

Apply the same fix to the Stylus `withdraw_fee` implementation. Optionally, enforce the check at parse time in `parseWithdrawFeePayload`.

### Proof of Concept

1. Governance constructs a `WithdrawFee` VAA with `targetAddress = address(0)`, `value = X`, `expo = 0`.
2. The VAA is signed by the current Wormhole guardian set and submitted to `executeGovernanceInstruction`.
3. `parseWithdrawFeePayload` decodes `wf.targetAddress = address(0)` with no rejection.
4. `withdrawFee` executes `address(0).call{value: X}("")` — this call succeeds in EVM.
5. `accruedFeesInWei` (or the contract's ETH balance) is decremented by `X`; the ETH is permanently burned.
6. `FeeWithdrawn(address(0), X)` is emitted; no revert occurs.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol (L264-272)
```text
    function withdrawFee(WithdrawFeePayload memory payload) internal {
        if (payload.fee > address(this).balance)
            revert PythErrors.InsufficientFee();

        (bool success, ) = payload.targetAddress.call{value: payload.fee}("");
        require(success, "Failed to withdraw fees");

        emit FeeWithdrawn(payload.targetAddress, payload.fee);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/PythGovernanceInstructions.sol (L255-274)
```text
    /// @dev Parse a WithdrawFeePayload (action 9) with minimal validation
    function parseWithdrawFeePayload(
        bytes memory encodedPayload
    ) public pure returns (WithdrawFeePayload memory wf) {
        uint index = 0;

        wf.targetAddress = address(encodedPayload.toAddress(index));
        index += 20;

        uint64 val = encodedPayload.toUint64(index);
        index += 8;

        uint64 expo = encodedPayload.toUint64(index);
        index += 8;

        wf.fee = uint256(val) * uint256(10) ** uint256(expo);

        if (encodedPayload.length != index)
            revert PythErrors.InvalidGovernanceMessage();
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

**File:** target_chains/stylus/contracts/pyth-receiver/src/governance.rs (L226-253)
```rust
    fn withdraw_fee(
        &mut self,
        value: u64,
        expo: u64,
        target_address: Address,
    ) -> Result<(), PythReceiverError> {
        let fee_to_withdraw =
            U256::from(value).saturating_mul(U256::from(10).pow(U256::from(expo)));
        let current_balance = self.vm().balance(self.vm().contract_address());

        if current_balance < fee_to_withdraw {
            return Err(PythReceiverError::InsufficientFee);
        }

        self.vm()
            .transfer_eth(target_address, fee_to_withdraw)
            .map_err(|_| PythReceiverError::InsufficientFee)?;

        log(
            self.vm(),
            crate::FeeWithdrawn {
                target_address,
                fee_amount: fee_to_withdraw,
            },
        );

        Ok(())
    }
```
