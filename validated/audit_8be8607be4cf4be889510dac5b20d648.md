### Title
Publisher Stake Account Change Without Advancing Delegation Record Causes Loss of Pending Self-Delegation Rewards — (File: `governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

---

### Summary

The `set_publisher_stake_account` instruction in the Pyth integrity pool program allows a publisher (or the `reward_program_authority`) to replace their associated stake account in `pool_data` without requiring that the delegation record for the current stake account has been advanced to the current epoch. Because the delegation record PDA is seeded by `["delegation_record", publisher, stake_account_positions]`, the old delegation record becomes permanently orphaned after the switch, and any pending self-delegation rewards accrued since the last `advance_delegation_record` call are irrecoverably lost.

---

### Finding Description

The integrity pool program stores each publisher's stake account in `pool_data.publisherStakeAccounts[index]`. The `advance_delegation_record` instruction reads this field to compute the publisher's self-delegation and distribute rewards to the publisher's stake account custody.

The `set_publisher_stake_account` instruction account list (from the IDL) is:

```
signer, publisher, pool_data, pool_config,
new_stake_account_positions_option (optional),
current_stake_account_positions_option (optional)
```

Critically, **`delegation_record` is absent from this account list**. This means the instruction cannot read or verify the state of the publisher's delegation record before executing the switch. The only guards present are:

- `PublisherOrRewardAuthorityNeedsToSign` (error 6001) — access control
- `CurrentStakeAccountShouldBeUndelegated` (error 6009) — checks that the current stake account has no active delegations
- `NewStakeAccountShouldBeUndelegated` (error 6010) — checks that the new stake account has no active delegations

The `CurrentStakeAccountShouldBeUndelegated` check prevents switching while positions are in the `LOCKED` state, but positions in a `LOCKING` (warmup) or `UNLOCKING` (cooldown) state may satisfy the "undelegated" condition while still having unclaimed rewards in the delegation record. Because `delegation_record` is not passed to `set_publisher_stake_account`, the program cannot enforce that `advance_delegation_record` has been called up to the current epoch before the switch.

After the switch, `pool_data.publisherStakeAccounts[index]` points to the new stake account. Any subsequent call to `advance_delegation_record` for the publisher will use the new stake account for self-delegation calculations. The old delegation record — keyed by `["delegation_record", publisher, old_stake_account]` — will never be advanced again, and the pending rewards it represents are permanently lost.

---

### Impact Explanation

**Impact: Medium**

A publisher who changes their stake account while holding unadvanced delegation record epochs loses all pending self-delegation rewards for those epochs. These rewards are PYTH tokens that were legitimately earned but never distributed. The tokens remain locked in the pool reward custody and cannot be claimed by the publisher. The loss is permanent and proportional to the number of unadvanced epochs multiplied by the publisher's self-delegation size and reward rate.

---

### Likelihood Explanation

**Likelihood: Medium**

Publishers have a legitimate operational reason to change their stake account (e.g., key rotation, account migration). The `set_publisher_stake_account` instruction is exposed as a normal operational function callable by the publisher themselves. There is no on-chain warning or forced pre-condition requiring `advance_delegation_record` to be called first. A publisher who is unaware of this requirement will silently lose pending rewards.

---

### Recommendation

Add a check inside `set_publisher_stake_account` that requires the publisher's delegation record to be advanced to the current epoch before allowing the stake account switch. Concretely:

1. Pass the `delegation_record` PDA (seeded by `["delegation_record", publisher, current_stake_account_positions]`) as a required account in `set_publisher_stake_account`.
2. Read `delegation_record.last_epoch` and compare it to the current epoch. Revert with an `OutdatedPublisherAccounting`-style error if they differ.

Alternatively, document the requirement prominently and enforce it at the SDK/CLI layer by automatically calling `advance_delegation_record` before `set_publisher_stake_account`.

---

### Proof of Concept

1. Publisher P has self-delegated tokens in stake account `SA_old` since epoch `E`.
2. At epoch `E+5`, P calls `advance_delegation_record` for the last time, advancing the record to epoch `E+5`.
3. At epoch `E+8`, P decides to rotate to a new stake account `SA_new`.
4. P unstakes from `SA_old` (positions enter cooldown / `UNLOCKING` state). The `CurrentStakeAccountShouldBeUndelegated` check passes.
5. P calls `set_publisher_stake_account(current=SA_old, new=SA_new)`. The instruction succeeds because `delegation_record` is not checked.
6. `pool_data.publisherStakeAccounts[index]` now points to `SA_new`.
7. The delegation record `["delegation_record", P, SA_old]` still has `last_epoch = E+5`. Epochs `E+6`, `E+7`, `E+8` are unadvanced.
8. No future call to `advance_delegation_record` will ever use `SA_old` again (since `pool_data` now points to `SA_new`).
9. The rewards for epochs `E+6` through `E+8` are permanently lost.

**Relevant IDL evidence — `set_publisher_stake_account` account list (no `delegation_record`):** [1](#0-0) 

**`advance_delegation_record` uses `publisher_stake_account_positions` from `pool_data` to compute self-delegation rewards:** [2](#0-1) 

**`delegation_record` PDA is seeded by `(publisher, stake_account_positions)` — becomes orphaned after stake account switch:** [3](#0-2) 

**SDK confirms `advance_delegation_record` reads `publisherStakeAccounts[index]` from `pool_data` at call time:** [4](#0-3) [5](#0-4) 

**Error codes confirm `set_publisher_stake_account` only checks delegation state, not epoch advancement:** [6](#0-5)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L72-90)
```json
      "name": "OutdatedDelegatorAccounting"
    },
    {
      "code": 6009,
      "name": "CurrentStakeAccountShouldBeUndelegated"
    },
    {
      "code": 6010,
      "name": "NewStakeAccountShouldBeUndelegated"
    },
    {
      "code": 6011,
      "name": "PublisherStakeAccountMismatch"
    },
    {
      "code": 6012,
      "name": "ThisCodeShouldBeUnreachable"
    },
    {
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L271-346)
```json
            "seeds": [
              {
                "kind": "const",
                "value": [99, 117, 115, 116, 111, 100, 121]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          },
          "writable": true
        },
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
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
        {
          "name": "delegation_record",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  100, 101, 108, 101, 103, 97, 116, 105, 111, 110, 95, 114, 101,
                  99, 111, 114, 100
                ]
              },
              {
                "kind": "account",
                "path": "publisher"
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          },
          "writable": true
        },
        {
          "address": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
          "name": "token_program"
        },
        {
          "address": "11111111111111111111111111111111",
          "name": "system_program"
        }
      ],
      "args": [],
      "discriminator": [155, 43, 226, 175, 227, 115, 33, 88],
      "name": "advance_delegation_record",
      "returns": "u64"
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L721-761)
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
    },
```

**File:** governance/pyth_staking_sdk/src/utils/pool.ts (L37-41)
```typescript
      stakeAccount:
        poolData.publisherStakeAccounts[index] === undefined ||
        poolData.publisherStakeAccounts[index].equals(PublicKey.default)
          ? null // eslint-disable-line unicorn/no-null
          : poolData.publisherStakeAccounts[index],
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L739-756)
```typescript
    const advanceDelegationRecordInstructions = await Promise.all(
      filteredPublishers.map(({ pubkey, stakeAccount }) =>
        this.integrityPoolProgram.methods
          .advanceDelegationRecord()
          .accountsPartial({
            payer: payer ?? this.wallet.publicKey,
            publisher: pubkey,
            publisherStakeAccountCustody: stakeAccount
              ? getStakeAccountCustodyAddress(stakeAccount)
              : null, // eslint-disable-line unicorn/no-null
            publisherStakeAccountPositions: stakeAccount,
            stakeAccountCustody: getStakeAccountCustodyAddress(
              stakeAccountPositions,
            ),
            stakeAccountPositions,
          })
          .instruction(),
      ),
```
