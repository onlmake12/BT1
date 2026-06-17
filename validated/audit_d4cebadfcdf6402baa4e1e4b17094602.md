### Title
Staking Program `freeze` Blocks `undelegate` CPI in Integrity-Pool, Permanently Locking OIS Positions — (`governance/pyth_staking_sdk/src/idl/integrity-pool.json`, `governance/pyth_staking_sdk/src/idl/staking.json`)

---

### Summary

The Pyth Oracle Integrity Staking (OIS) system's `undelegate` instruction in the integrity-pool program makes a Cross-Program Invocation (CPI) into the staking program's `close_position` instruction. The staking program's `GlobalConfig` contains a `freeze: bool` flag, and the program defines a `Frozen` error (code 6017). When the staking program is frozen, the CPI from `undelegate` into `close_position` reverts with `Frozen`, making it impossible for staking users to exit their OIS delegated positions. This is a direct analog to M-03: a paused/frozen sub-module blocks a critical user-exit operation.

---

### Finding Description

The integrity-pool program exposes an `undelegate` instruction that allows staking users to remove their delegated stake from a publisher pool. Inspecting the `undelegate` account list in the IDL reveals that it passes the staking program's `config_account` PDA, `stake_account_positions`, `stake_account_metadata`, `stake_account_custody`, and the `staking_program` address (`pytS9TjG1qyAZypk7n8rw8gfW9sUaqqYyMhJQ4E7JCQ`) as accounts: [1](#0-0) 

This account layout is consistent with the integrity-pool program making a CPI to the staking program's `close_position` instruction to close the delegated position on behalf of the user.

The staking program's `GlobalConfig` struct contains a `freeze` boolean field: [2](#0-1) 

The staking program defines a `Frozen` error (code 6017, "Protocol is frozen"): [3](#0-2) 

The `close_position` instruction in the staking program takes `config` as an account: [4](#0-3) 

When the staking program is frozen (i.e., `config.freeze == true`), `close_position` reverts with `Frozen`. Because `undelegate` in the integrity-pool program calls `close_position` via CPI, the entire `undelegate` transaction reverts. There is no fallback path: the user's delegated OIS position is stuck for the duration of the freeze.

The same applies to `withdraw_stake`, which also loads `config`: [5](#0-4) 

---

### Impact Explanation

When governance freezes the staking program (e.g., in response to a critical vulnerability or emergency), all staking users who have delegated to OIS publishers are unable to call `undelegate` to exit their positions. Their PYTH tokens remain locked in the integrity-pool delegation for the entire duration of the freeze. If the freeze is prolonged, users cannot respond to slashing risk, publisher misbehavior, or market conditions. This constitutes a temporary but potentially severe loss of user funds access — a DoS on the critical user-exit path.

---

### Likelihood Explanation

The `freeze` flag exists precisely for emergency use. It is realistic that governance would freeze the staking program in response to a discovered vulnerability while simultaneously needing users to be able to exit OIS positions (e.g., to prevent further slashing exposure). The two actions — freezing the staking program and allowing OIS exits — are in direct conflict under the current design. This is the same scenario described in M-03: an emergency freeze of one module inadvertently blocks the unwind of another.

---

### Recommendation

The integrity-pool `undelegate` instruction should handle the case where the staking program is frozen. One approach is to check the `freeze` flag from the passed `config_account` before making the CPI, and if frozen, update only the integrity-pool's internal delegation accounting (marking the position as closed in `pool_data` and `delegation_record`) without invoking `close_position`. The staking-side position closure could then be deferred to a separate instruction callable once the freeze is lifted. Alternatively, the staking program could exempt `close_position` calls originating from the integrity-pool program from the freeze check, since undelegation is a user-protective exit action.

---

### Proof of Concept

1. User delegates PYTH tokens to a publisher via `delegate` in the integrity-pool program. A position is recorded in `stake_account_positions` with `targetWithParameters = IntegrityPool { publisher }`.
2. Governance detects an emergency and calls `update_config` on the staking program, setting `config.freeze = true`.
3. User attempts to call `undelegate` (integrity-pool) to exit their position.
4. `undelegate` makes a CPI to `close_position` (staking program), passing `config_account`.
5. `close_position` reads `config.freeze == true` and returns `Err(ErrorCode::Frozen)`.
6. The CPI fails, `undelegate` reverts, and the user's OIS position remains locked.
7. The user cannot exit until governance unfreezes the staking program — which may be delayed indefinitely if the freeze is due to an unresolved critical issue.

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L961-1080)
```json
    {
      "accounts": [
        {
          "name": "owner",
          "signer": true,
          "writable": true
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
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "config_account",
          "pda": {
            "program": {
              "kind": "account",
              "path": "staking_program"
            },
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "stake_account_positions",
          "writable": true
        },
        {
          "docs": [
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "stake_account_metadata",
          "pda": {
            "program": {
              "kind": "account",
              "path": "staking_program"
            },
            "seeds": [
              {
                "kind": "const",
                "value": [
                  115, 116, 97, 107, 101, 95, 109, 101, 116, 97, 100, 97, 116,
                  97
                ]
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
            "CHECK : This AccountInfo is safe because it's a checked PDA"
          ],
          "name": "stake_account_custody",
          "pda": {
            "program": {
              "kind": "account",
              "path": "staking_program"
            },
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
          }
        },
        {
          "address": "pytS9TjG1qyAZypk7n8rw8gfW9sUaqqYyMhJQ4E7JCQ",
          "name": "staking_program"
        },
        {
          "address": "11111111111111111111111111111111",
          "name": "system_program"
        }
      ],
      "args": [
        {
          "name": "position_index",
          "type": "u8"
        },
        {
          "name": "amount",
          "type": "u64"
        }
      ],
      "discriminator": [131, 148, 180, 198, 91, 104, 42, 238],
      "name": "undelegate"
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L119-123)
```json
    {
      "code": 6017,
      "msg": "Protocol is frozen",
      "name": "Frozen"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L495-505)
```json
        {
          "name": "config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L1783-1797)
```json
        {
          "name": "config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "address": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
          "name": "token_program"
        }
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L1844-1847)
```json
          {
            "name": "freeze",
            "type": "bool"
          },
```
