### Title
Entropy Provider Can Permanently Lock User Fees by Withholding Reveal — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the provider's fee is credited to `providerInfo.accruedFeesInWei` immediately at request time, before any reveal occurs. The `revealWithCallback` function requires the provider's secret hash-chain preimage (`providerContribution`), which only the provider knows. If the provider never calls `revealWithCallback`, the user's funds are permanently locked in the contract with no cancel, timeout, or refund path available to the user.

---

### Finding Description

When a user calls `requestWithCallback` or `requestV2`, the internal `requestHelper` function immediately credits the provider's fee:

```solidity
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The user's request is then stored on-chain, waiting for the provider to call `revealWithCallback` with their secret preimage `providerContribution`. The `revealHelper` function verifies this preimage against the stored commitment:

```solidity
bytes32 providerCommitment = constructProviderCommitment(
    req.numHashes,
    providerContribution
);
if (keccak256(bytes.concat(userCommitment, providerCommitment)) != req.commitment)
    revert EntropyErrors.IncorrectRevelation();
``` [2](#0-1) 

Only the provider possesses the correct `providerContribution` value (the preimage of their hash chain). No other party can supply it. The `revealWithCallback` function is permissionless in terms of caller, but is gated on knowledge of this secret: [3](#0-2) 

The entire `Entropy.sol` contract and its interface expose no `cancelRequest`, `requestRefund`, or timeout-based recovery function for users. The only withdrawal functions are `withdraw()` and `withdrawAsFeeManager()`, both of which are exclusively for providers: [4](#0-3) 

There is no analogous user-facing withdrawal path anywhere in the contract.

---

### Impact Explanation

A provider who registers, accepts requests, and then withholds the reveal:

1. Collects fees immediately (credited at request time, withdrawable at any time via `withdraw()`).
2. Leaves all in-flight user requests permanently unresolvable.
3. Causes permanent loss of user funds (both the provider fee and the Pyth protocol fee are irrecoverable by the user).
4. Prevents any dependent application logic (the `_entropyCallback`) from ever executing, blocking downstream protocol state for any consumer contract that depends on the random number.

This is a direct fund-locking impact on users with no recovery path.

---

### Likelihood Explanation

- Any address can permissionlessly call `register()` to become a provider.
- A provider has a direct financial incentive to withhold: they are paid before they reveal, so non-revelation is strictly profitable for a malicious provider.
- The Fortuna off-chain service is the reference provider, but the on-chain contract imposes no liveness obligation or slashing mechanism on any provider.
- A provider going offline (non-malicious but unresponsive) produces the same permanent lock for users.

---

### Recommendation

Add a user-callable `cancelRequest` function that:
- Is callable only after a configurable timeout (e.g., `block.number > req.blockNumber + TIMEOUT_BLOCKS`).
- Refunds the user's fee (both the provider portion and the Pyth fee).
- Clears the request from storage.

This mirrors the recommendation in the referenced report: allow users to unilaterally resolve their stuck state after sufficient time has elapsed, without requiring any action from the provider.

---

### Proof of Concept

1. Attacker registers as a provider via `register(feeInWei, commitment, ..., chainLength, uri)`.
2. Victim calls `requestWithCallback(attackerProvider, userContribution)` paying `getFee(attackerProvider)`.
3. Inside `requestHelper`, `providerInfo.accruedFeesInWei += providerFee` executes immediately — attacker is paid.
4. Attacker never calls `revealWithCallback`. No other party can supply the correct `providerContribution`.
5. Attacker calls `withdraw(providerFee)` to extract their fee.
6. Victim's request remains in storage indefinitely. `getRequestV2(attackerProvider, sequenceNumber)` returns a live request with no resolution path.
7. Victim has no function to call to recover funds or cancel the request. [5](#0-4)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L214-284)
```text
    function requestHelper(
        address provider,
        bytes32 userCommitment,
        bool useBlockhash,
        bool isRequestWithCallback,
        uint32 callbackGasLimit
    ) internal returns (EntropyStructsV2.Request storage req) {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];
        if (_state.providers[provider].sequenceNumber == 0)
            revert EntropyErrors.NoSuchProvider();

        // Assign a sequence number to the request
        uint64 assignedSequenceNumber = providerInfo.sequenceNumber;
        if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.OutOfRandomness();
        providerInfo.sequenceNumber += 1;

        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);

        // Store the user's commitment so that we can fulfill the request later.
        // Warning: this code needs to overwrite *every* field in the request, because the returned request can be
        // filled with arbitrary data.
        req = allocRequest(provider, assignedSequenceNumber);
        req.provider = provider;
        req.sequenceNumber = assignedSequenceNumber;
        req.numHashes = SafeCast.toUint32(
            assignedSequenceNumber -
                providerInfo.currentCommitmentSequenceNumber
        );
        if (
            providerInfo.maxNumHashes != 0 &&
            req.numHashes > providerInfo.maxNumHashes
        ) {
            revert EntropyErrors.LastRevealedTooOld();
        }
        req.commitment = keccak256(
            bytes.concat(userCommitment, providerInfo.currentCommitment)
        );
        req.requester = msg.sender;

        req.blockNumber = SafeCast.toUint64(block.number);
        req.useBlockhash = useBlockhash;

        req.callbackStatus = isRequestWithCallback
            ? EntropyStatusConstants.CALLBACK_NOT_STARTED
            : EntropyStatusConstants.CALLBACK_NOT_NECESSARY;
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
            // This check does two important things:
            // 1. Providers have a minimum fee set for their defaultGasLimit. If users request less gas than that,
            //    they still pay for the full gas limit. So we may as well give them the full limit here.
            // 2. If a provider has a defaultGasLimit != 0, we need to ensure that all requests have a >0 gas limit
            //    so that we opt-in to the new callback failure state flow.
            req.gasLimit10k = roundTo10kGas(
                callbackGasLimit < providerInfo.defaultGasLimit
                    ? providerInfo.defaultGasLimit
                    : callbackGasLimit
            );
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L400-408)
```text
        bytes32 providerCommitment = constructProviderCommitment(
            req.numHashes,
            providerContribution
        );
        bytes32 userCommitment = constructUserCommitment(userContribution);
        if (
            keccak256(bytes.concat(userCommitment, providerCommitment)) !=
            req.commitment
        ) revert EntropyErrors.IncorrectRevelation();
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L542-566)
```text
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override {
        EntropyStructsV2.Request storage req = findActiveRequest(
            provider,
            sequenceNumber
        );

        if (
            !(req.callbackStatus ==
                EntropyStatusConstants.CALLBACK_NOT_STARTED ||
                req.callbackStatus == EntropyStatusConstants.CALLBACK_FAILED)
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }

        bytes32 randomNumber;
        (randomNumber, ) = revealHelper(
            req,
            userContribution,
            providerContribution
        );
```
