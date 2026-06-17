### Title
Entropy Provider Can Selectively Reveal Random Numbers to Manipulate Outcomes — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

An Entropy provider, who can register permissionlessly, pre-commits to an entire hash chain of random values. Because the provider knows all future hash chain values in advance and faces no on-chain time constraint to reveal, they can compute the final random number off-chain before deciding whether to call `revealWithCallback`. This allows a malicious provider to selectively fulfill or withhold requests based on the computed outcome, directly manipulating the randomness delivered to consumers.

---

### Finding Description

The Entropy protocol assigns each user request a sequence number `i` from the provider's hash chain. The final random number is `r = hash(x_i, x_U)`, where `x_i` is the provider's pre-committed secret and `x_U` is the user's contribution.

The provider generates the entire hash chain offline during `register()`:

```
x_{N-1} = random()
x_i     = hash(x_{i+1})
``` [1](#0-0) 

Because the provider holds all `x_i` values, they can compute `r = hash(x_i, x_U)` the moment a user's `request` transaction is visible (mempool or on-chain). The `revealWithCallback` function imposes **no deadline** for the provider to reveal: [2](#0-1) 

The provider can therefore:

1. Observe the user's `userContribution` (`x_U`) from the pending or confirmed `request` transaction.
2. Compute `r = hash(x_i, x_U)` off-chain.
3. If `r` is unfavorable (e.g., the user would win a lottery), withhold the reveal indefinitely or rotate the commitment via `register()` to change which `x_i` the user receives.
4. If `r` is favorable, call `revealWithCallback` immediately.

The `register()` function is public and callable at any time, allowing commitment rotation: [3](#0-2) 

The contract's own comment acknowledges the structural exposure: [4](#0-3) 

The protocol design documentation further confirms:

> *"Providers who observe user transactions can manipulate the result by inserting additional requests or rotating their commitment."* [5](#0-4) 

---

### Impact Explanation

Any Entropy consumer that relies on the randomness for high-value outcomes — NFT trait assignment, on-chain lotteries, game outcomes, random airdrops — is vulnerable to outcome manipulation by a malicious provider. The provider can:

- **Censor** unfavorable outcomes by never revealing, leaving the user's request permanently stuck.
- **Cherry-pick** favorable outcomes by rotating the commitment (via `register()`) until a desired `x_i` is in position, then revealing.

Because provider registration is permissionless and the fee can be set competitively low to attract users, a malicious provider can accumulate a large user base before exploiting this.

---

### Likelihood Explanation

- **Provider registration is permissionless**: anyone can call `register()` with any fee and commitment.
- **No on-chain time constraint** forces the provider to reveal within a bounded window.
- **The provider's entire hash chain is known to them in advance**, making off-chain outcome computation trivial.
- A colluding operator (e.g., a lottery contract that is also the Entropy provider) has direct financial incentive to exploit this.

Likelihood: **Medium** — requires a malicious provider, but the barrier to becoming a provider is zero.

---

### Recommendation

1. **Enforce a reveal deadline**: Add an on-chain timeout after which any party can trigger a default outcome or refund, removing the provider's ability to indefinitely withhold.
2. **Penalize non-revelation**: Slash or forfeit the provider's staked collateral if they fail to reveal within the deadline.
3. **Warn consumers explicitly**: Document that using a non-default, unaudited provider transfers full trust to that provider for outcome integrity.
4. **Consider VDF or threshold schemes** for high-value use cases where provider honesty cannot be assumed.

---

### Proof of Concept

1. Deploy a lottery contract that uses Entropy with a custom provider controlled by the attacker.
2. Register as a provider via `register()`, generating a known hash chain.
3. A user calls `requestWithCallback(attackerProvider, userRandomNumber)`.
4. Attacker computes `r = combineRandomValues(userRandomNumber, x_i, 0)` off-chain.
5. If `r` maps to a losing lottery ticket, attacker calls `register()` again (rotating commitment), incrementing `sequenceNumber`.
6. The user's request now maps to `x_{i+1}` from the new chain; attacker recomputes `r'`.
7. Repeat until `r'` maps to a winning ticket for the attacker's chosen address, then call `revealWithCallback`.

The `combineRandomValues` function used for off-chain pre-computation: [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L35-48)
```text
// Setup: The provider P computes a sequence of N random numbers, x_i (i = 0...N-1):
// x_{N-1} = random()
// x_i = hash(x_{i + 1})
// The provider commits to x_0 by posting it to the contract. Each random number in the sequence can then be
// verified against the previous one in the sequence by hashing it, i.e., hash(x_i) == x_{i - 1}
//
// Request: To produce a random number, the following steps occur.
// 1. The user randomly samples their contribution x_U and submits it to the contract
// 2. The contract remembers x_U and assigns it an incrementing sequence number i, representing which
//    of the provider's random numbers the user will receive.
// 3. The provider submits a transaction to the contract revealing their contribution x_i to the contract.
// 4. The contract verifies hash(x_i) == x_{i-1} to prove that x_i is the i'th random number.
//    The contract stores x_i as the i'th random number to reuse for future verifications.
// 5. If the condition above is satisfied, the random number r = hash(x_i, x_U).
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L51-54)
```text
// This protocol has the same security properties as the 2-party randomness protocol above: as long as either
// the provider or user is honest, the number r is random. Note that this analysis assumes that
// providers cannot frontrun user transactions -- a dishonest provider who frontruns user transaction can
// manipulate the result.
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L111-145)
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
    }
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L950-958)
```text
    function combineRandomValues(
        bytes32 userRandomness,
        bytes32 providerRandomness,
        bytes32 blockHash
    ) public pure override returns (bytes32 combinedRandomness) {
        combinedRandomness = keccak256(
            abi.encodePacked(userRandomness, providerRandomness, blockHash)
        );
    }
```

**File:** apps/developer-hub/content/docs/entropy/protocol-design.mdx (L52-55)
```text
- Providers are trusted to reveal their random number $$(x_i)$$ regardless of what the final result $$(r)$$ is. Providers can compute $$(r)$$ off-chain before they reveal $$(x_i)$$, which permits a censorship attack.
- Providers are trusted not to front-run user transactions (via the mempool or colluding with the validator). Providers who observe user transactions can manipulate the result by inserting additional reuests or rotating their commitment.
- Providers are trusted not to keep their hash chain a secret. Anyone with the hash chain can predict the result of a randomness request before it is requested,
  and therefore manipulate the result. This applies both to users of the protocol as well as blockchain validators who can use this information to manipulate the on-chain PRNG or reorder user transactions.
```
