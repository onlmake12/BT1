### Title
Provider Fee Credited at Request Time With No On-Chain Fulfillment Incentive or Refund Path — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The Pyth Entropy contract credits the provider's fee at **request time**, not at **reveal/fulfillment time**. There is no on-chain mechanism — no timeout, no slashing, no refund — to enforce that a registered provider ever calls `revealWithCallback`. The system relies entirely on the off-chain Fortuna keeper service to complete the commit/reveal cycle, mirroring the exact centralization risk described in the reference report.

---

### Finding Description

In `Entropy.sol`, `requestHelper` is the internal function called by every `request*` and `requestV2*` entry point. At lines 236–239, the provider's fee is immediately credited to `providerInfo.accruedFeesInWei` at the moment the user submits their request:

```solidity
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;          // ← credited NOW
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The provider can withdraw this balance at any time via `withdraw()` or `withdrawAsFeeManager()`, with no condition on having fulfilled any pending requests. [2](#0-1) 

The reveal step — `revealWithCallback` — is permissionless (anyone may call it), but there is **no on-chain obligation, deadline, or penalty** that compels the provider to do so. The contract itself acknowledges this in `allocRequest`:

> "There is a chance that some requests never get revealed and remain active forever." [3](#0-2) 

There is no timeout, no expiry, no user-callable cancellation, and no refund path in the contract. The `EntropyState` stores requests indefinitely with no TTL field. [4](#0-3) 

The entire fulfillment liveness guarantee is delegated to the off-chain **Fortuna keeper** (`apps/fortuna/`), a centralized service operated by Pyth that monitors events and calls `reveal_with_callback` on behalf of providers. [5](#0-4) 

The keeper's own documentation acknowledges it requires a funded wallet and a fee-manager private key to operate; if the keeper's balance falls below `min_keeper_balance`, it attempts to withdraw from accrued provider fees — but this is entirely off-chain logic with no on-chain fallback. [6](#0-5) 

---

### Impact Explanation

Any address can permissionlessly register as a provider via `register()`. A malicious or negligent registered provider can:

1. Accept user requests and collect fees (credited immediately on-chain).
2. Never call `revealWithCallback`.
3. Withdraw all accrued fees via `withdraw()`.

Users who requested randomness from that provider:
- Permanently lose their paid fees (no refund path exists).
- Never receive their random number or callback.
- Have their dependent application (game, lottery, NFT mint) permanently stuck in a pending state.

Even for the default Pyth provider, if the Fortuna keeper service experiences an outage, misconfiguration, or wallet depletion, all in-flight requests stall with no on-chain recovery path for users.

---

### Likelihood Explanation

- **Third-party providers**: Any address can register. A provider that front-runs its own exit (collect fees, stop revealing) faces no on-chain consequence.
- **Default provider liveness**: The Fortuna keeper is a single off-chain service. Its config exposes `min_keeper_balance` and fee-withdrawal logic, meaning keeper downtime directly causes unfulfilled requests.
- **No user recourse**: Unlike the Beam Network report where the suggestion was to add a gas station or conditional compensation, Pyth Entropy has no analogous on-chain fallback at all.

---

### Recommendation

1. **Defer fee credit to reveal time**: Credit `providerInfo.accruedFeesInWei` inside `revealHelper` (or `revealWithCallback`) rather than in `requestHelper`. This creates a direct on-chain financial incentive for providers to reveal.
2. **Add a request expiry / user refund path**: Allow users to reclaim their fee after a configurable number of blocks if the request remains unfulfilled.
3. **Document the trust assumption on-chain**: At minimum, add a NatSpec comment to `requestHelper` and `register()` stating that providers are trusted to reveal and that no on-chain enforcement exists, so users can make informed provider choices.

---

### Proof of Concept

1. Attacker calls `register(feeInWei, commitment, ..., chainLength, uri)` to become a provider.
2. Victim calls `requestV2(attackerAddress, userRandomNumber, gasLimit)` paying `getFeeV2(attackerAddress, gasLimit)` wei.
3. `requestHelper` executes: `providerInfo.accruedFeesInWei += providerFee` — attacker's balance is immediately incremented.
4. Attacker calls `withdraw(providerFee)` — funds transferred to attacker.
5. Attacker never calls `revealWithCallback`. No on-chain mechanism forces them to.
6. Victim's request sits in `_state.requests` or `_state.requestsOverflow` indefinitely. No timeout, no refund, no cancellation function exists.
7. Victim's dependent contract never receives its `entropyCallback`, permanently blocking application logic. [7](#0-6) [8](#0-7) [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L111-144)
```text
    function register(
        uint128 feeInWei,
        bytes32 commitment,
        bytes calldata commitmentMetadata,
        uint64 chainLength,
        bytes calldata uri
    ) public override {
        if (chainLength == 0) revert EntropyErrors.AssertionFailure();

        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        // NOTE: this method implementation depends on the fact that ProviderInfo will be initialized to all-zero.
        // Specifically, accruedFeesInWei is intentionally not set. On initial registration, it will be zero,
        // then on future registrations, it will be unchanged. Similarly, provider.sequenceNumber defaults to 0
        // on initial registration.

        provider.feeInWei = feeInWei;

        provider.originalCommitment = commitment;
        provider.originalCommitmentSequenceNumber = provider.sequenceNumber;
        provider.currentCommitment = commitment;
        provider.currentCommitmentSequenceNumber = provider.sequenceNumber;
        provider.commitmentMetadata = commitmentMetadata;
        provider.endSequenceNumber = provider.sequenceNumber + chainLength;
        provider.uri = uri;

        provider.sequenceNumber += 1;

        emit EntropyEvents.Registered(
            EntropyStructConverter.toV1ProviderInfo(provider)
        );
        emit EntropyEventsV2.Registered(msg.sender, bytes(""));
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L150-165)
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

```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L233-239)
```text
        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L1058-1063)
```text
            // It is important that this code overflows the *prior* request to the mapping, and not the new request.
            // There is a chance that some requests never get revealed and remain active forever. We do not want such
            // requests to fill up all of the space in the array and cause all new requests to incur the higher gas cost
            // of the mapping.
            //
            // This operation is expensive, but should be rare. If overflow happens frequently, increase
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L33-34)
```text
        EntropyStructsV2.Request[32] requests;
        mapping(bytes32 => EntropyStructsV2.Request) requestsOverflow;
```

**File:** apps/fortuna/src/keeper/process_event.rs (L141-146)
```rust
    let contract_call = contract.reveal_with_callback(
        event.provider_address,
        event.sequence_number,
        event.user_random_number,
        provider_revelation,
    );
```

**File:** apps/fortuna/src/keeper/fee.rs (L142-161)
```rust
/// Withdraws accumulated fees in the contract as needed to maintain the balance of the keeper wallet.
pub async fn withdraw_fees_if_necessary(
    contract_as_fee_manager: Arc<InstrumentedSignablePythContract>,
    provider_address: Address,
    keeper_address: Address,
    other_keeper_addresses: Vec<Address>,
    min_balance: U256,
) -> Result<()> {
    let provider = contract_as_fee_manager.provider();
    let fee_manager_wallet = contract_as_fee_manager.wallet();

    let keeper_balance = provider
        .get_balance(keeper_address, None)
        .await
        .map_err(|e| anyhow!("Error while getting balance. error: {:?}", e))?;

    // Only withdraw if our balance is below the minimum threshold
    if keeper_balance >= min_balance {
        return Ok(());
    }
```
