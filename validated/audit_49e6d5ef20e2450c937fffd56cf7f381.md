### Title
Any Signer Can Redirect a Publisher's Stake Account, Stealing Self-Staking Rewards - (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

---

### Summary

The `set_publisher_stake_account` instruction in the Pyth Oracle Integrity Staking (OIS) integrity-pool program accepts any arbitrary `signer` without verifying that the signer is the publisher (or an authorized authority over the publisher). This allows an unprivileged attacker to redirect any publisher's registered stake account to their own, causing the publisher's self-staking OIS rewards to be credited to the attacker's custody account.

---

### Finding Description

The `set_publisher_stake_account` instruction's account list, as defined in the IDL, is:

- `signer` — must sign, but **no constraint** linking it to `publisher`
- `publisher` — **not a signer**, only checked to exist in `pool_data`
- `pool_data` — writable
- `newStakeAccountPositionsOption` — optional, the new stake account to associate
- `currentStakeAccountPositionsOption` — optional, the current stake account [1](#0-0) 

The `publisher` account carries the comment `"CHECK : The publisher will be checked against data in the pool_data"` — meaning only that the publisher key must exist in the pool, **not** that the signer is authorized to act on behalf of that publisher. [2](#0-1) 

There is no `relations` constraint, no `signer == publisher` check, and no `reward_program_authority` requirement. Compare this to instructions like `create_slash_event`, which explicitly require `reward_program_authority` as a signer.

The SDK client confirms this is exposed as a user-callable function — `this.wallet.publicKey` becomes the signer, and any wallet can pass any `publisher` key: [3](#0-2) 

The staking app also exposes `reassignPublisherAccount` as a user-facing action with no additional authorization: [4](#0-3) 

---

### Impact Explanation

The `pool_data` account stores `publisherStakeAccounts[publisher_index]` — the stake account associated with each publisher for self-staking reward distribution. [5](#0-4) 

When `advance_delegation_record` is called, it distributes the publisher's self-staking rewards to `publisher_stake_account_custody`, which is derived from `publisher_stake_account_positions` stored in `pool_data`: [6](#0-5) 

If an attacker calls `set_publisher_stake_account` with their own stake account as `newStakeAccountPositionsOption` for a victim publisher, all subsequent `advance_delegation_record` calls will send the publisher's self-staking OIS rewards to the attacker's custody. The attacker can then withdraw those tokens.

---

### Likelihood Explanation

The entry path is fully permissionless — any Solana wallet can submit this transaction. No privileged role, leaked key, or social engineering is required. The attacker only needs to know the publisher's public key (which is public on-chain in `pool_data`). The attack is cheap (only transaction fees) and can be executed repeatedly.

---

### Recommendation

Add a constraint requiring that `signer` must equal `publisher`, or that `signer` must be the `reward_program_authority` stored in `pool_config`. In Anchor, this would be expressed as a `constraint = signer.key() == publisher.key()` or a `has_one = reward_program_authority` on the `pool_config` account. The `set_publisher_stake_account` instruction should only be callable by the publisher themselves or a designated governance authority.

---

### Proof of Concept

1. Attacker identifies victim publisher `P` in `pool_data.publishers[]`.
2. Attacker creates their own stake account `A`.
3. Attacker calls `set_publisher_stake_account` with:
   - `signer` = attacker's wallet (signs)
   - `publisher` = `P`
   - `newStakeAccountPositionsOption` = `A`
   - `currentStakeAccountPositionsOption` = `P`'s current stake account
4. `pool_data.publisherStakeAccounts[P_index]` is now set to `A`.
5. Any subsequent call to `advance_delegation_record` for publisher `P` sends `P`'s self-staking rewards to `A`'s custody.
6. Attacker withdraws the accumulated rewards from `A`.

**Note:** The actual on-chain Rust source for the integrity-pool program is not present in this repository (only the IDL is available). The finding is based on the IDL account constraints. If the on-chain program contains an undocumented runtime check (e.g., `signer == publisher`), this finding would not apply. However, the absence of any such constraint in the IDL, combined with the permissionless SDK exposure, makes this a credible vulnerability warranting on-chain verification.

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L291-310)
```json
          "name": "publisher_stake_account_positions",
          "optional": true
        },
        {
          "name": "publisher_stake_account_custody",
          "optional": true,
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 117, 115, 116, 111, 100, 121]
              },
              {
                "kind": "account",
                "path": "publisher_stake_account_positions"
              }
            ]
          },
          "writable": true
        },
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L721-760)
```json
    {
      "accounts": [
        {
          "name": "signer",
          "signer": true
        },
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
          "name": "pool_data",
          "relations": ["pool_config"],
          "writable": true
        },
        {
          "name": "pool_config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "new_stake_account_positions_option",
          "optional": true
        },
        {
          "name": "current_stake_account_positions_option",
          "optional": true
        }
      ],
      "args": [],
      "discriminator": [99, 46, 72, 132, 100, 235, 211, 117],
      "name": "set_publisher_stake_account"
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L827-844)
```typescript
  async setPublisherStakeAccount(
    publisher: PublicKey,
    stakeAccountPositions: PublicKey,
    newStakeAccountPositions: PublicKey | undefined,
  ) {
    const instruction = await this.integrityPoolProgram.methods
      .setPublisherStakeAccount()
      .accounts({
        currentStakeAccountPositionsOption: stakeAccountPositions,
        // eslint-disable-next-line unicorn/no-null
        newStakeAccountPositionsOption: newStakeAccountPositions ?? null,
        publisher,
      })
      .instruction();

    await sendTransaction([instruction], this.connection, this.wallet);
    return;
  }
```

**File:** apps/staking/src/api.ts (L409-420)
```typescript
export const reassignPublisherAccount = async (
  client: PythStakingClient,
  stakeAccount: PublicKey,
  targetAccount: PublicKey,
  publisherKey: PublicKey,
): Promise<void> => {
  return client.reassignPublisherStakeAccount(
    publisherKey,
    stakeAccount,
    targetAccount,
  );
};
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1693-1698)
```typescript
          {
            name: "publisherStakeAccounts";
            type: {
              array: ["pubkey", 1024];
            };
          },
```
