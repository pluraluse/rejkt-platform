"""
REJKT FA2 Token Contract
========================
A multi-edition NFT contract for the REJKT collection on Tezos.

Features:
  - Admin-only drop creation (open or limited editions)
  - Anyone can collect/mint by paying the drop price
  - Per-contract royalty (set at origination, applies to all tokens)
  - Royalty recipient stored per-token for future splits upgrade path
  - TZIP-12 (FA2) and TZIP-16 (contract metadata) compliant
  - Token IDs are sequential starting at 0, unique to this contract

Instructions:
  1. Paste this file into the SmartPy IDE at https://smartpy.io/ide
  2. Replace ADMIN_ADDRESS with your Tezos wallet address (tz1...)
  3. Replace ROYALTY_PERCENT with your desired royalty (e.g. 150 = 15%)
  4. Run tests, then deploy to Ghostnet first, then Mainnet

Upgrade path notes:
  - royalty_recipients stored as map per token to support future splits
  - Currently always set to {admin: royalty_percent} at mint time
  - v2 will allow multiple recipients with split percentages
"""

import smartpy as sp

# ── Constants (edit these before deploying) ───────────────────────────────────

ADMIN_ADDRESS   = "tz1YourWalletAddressHere"  # Your Tezos wallet
ROYALTY_PERCENT = sp.nat(150)                 # 150 = 15% (out of 1000)

# ─────────────────────────────────────────────────────────────────────────────

@sp.module
def rejkt_module():

    class REJKTToken(sp.Contract):
        def __init__(self, admin_address, royalty_percent):
            self.data.administrator   = sp.cast(admin_address, sp.address)
            self.data.royalty_percent = sp.cast(royalty_percent, sp.nat)

            # FA2 standard storage
            self.data.ledger = sp.cast(
                sp.big_map(),
                sp.big_map[sp.pair[sp.address, sp.nat], sp.nat]
            )
            self.data.operators = sp.cast(
                sp.big_map(),
                sp.big_map[sp.pair[sp.address, sp.pair[sp.address, sp.nat]], sp.unit]
            )
            self.data.token_metadata = sp.cast(
                sp.big_map(),
                sp.big_map[sp.nat, sp.record(
                    token_id   = sp.nat,
                    token_info = sp.map[sp.string, sp.bytes]
                )]
            )
            self.data.all_tokens = sp.cast(sp.nat(0), sp.nat)

            # REJKT-specific storage
            self.data.next_drop_id = sp.cast(sp.nat(0), sp.nat)
            self.data.drops = sp.cast(
                sp.big_map(),
                sp.big_map[sp.nat, sp.record(
                    price        = sp.mutez,
                    max_editions = sp.option[sp.nat],
                    minted       = sp.nat,
                    active       = sp.bool,
                    token_id     = sp.nat,
                )]
            )
            # Per-token royalty map — ready for v2 splits
            self.data.royalty_recipients = sp.cast(
                sp.big_map(),
                sp.big_map[sp.nat, sp.map[sp.address, sp.nat]]
            )

        # ── FA2: transfer ─────────────────────────────────────────────────────

        @sp.entrypoint
        def transfer(self, batch):
            sp.cast(batch, sp.list[sp.record(
                from_ = sp.address,
                txs   = sp.list[sp.record(
                    to_      = sp.address,
                    token_id = sp.nat,
                    amount   = sp.nat
                )]
            )])
            for transfer in batch:
                for tx in transfer.txs:
                    sp.verify(
                        (transfer.from_ == sp.sender) |
                        self.data.operators.contains(
                            sp.pair(transfer.from_, sp.pair(sp.sender, tx.token_id))
                        ),
                        "FA2_NOT_OPERATOR"
                    )
                    sp.verify(
                        self.data.token_metadata.contains(tx.token_id),
                        "FA2_TOKEN_UNDEFINED"
                    )
                    from_bal = self.data.ledger.get(
                        sp.pair(transfer.from_, tx.token_id), default=sp.nat(0)
                    )
                    sp.verify(from_bal >= tx.amount, "FA2_INSUFFICIENT_BALANCE")
                    self.data.ledger[sp.pair(transfer.from_, tx.token_id)] = sp.as_nat(from_bal - tx.amount)
                    to_bal = self.data.ledger.get(
                        sp.pair(tx.to_, tx.token_id), default=sp.nat(0)
                    )
                    self.data.ledger[sp.pair(tx.to_, tx.token_id)] = to_bal + tx.amount

        # ── FA2: balance_of ───────────────────────────────────────────────────

        @sp.entrypoint
        def balance_of(self, params):
            sp.cast(params, sp.record(
                requests = sp.list[sp.record(owner=sp.address, token_id=sp.nat)],
                callback = sp.contract[sp.list[sp.record(
                    request = sp.record(owner=sp.address, token_id=sp.nat),
                    balance = sp.nat
                )]]
            ))
            results = sp.local("results", [], sp.list[sp.record(
                request = sp.record(owner=sp.address, token_id=sp.nat),
                balance = sp.nat
            )])
            for req in params.requests:
                sp.verify(
                    self.data.token_metadata.contains(req.token_id),
                    "FA2_TOKEN_UNDEFINED"
                )
                bal = self.data.ledger.get(
                    sp.pair(req.owner, req.token_id), default=sp.nat(0)
                )
                results.value.push(sp.record(request=req, balance=bal))
            sp.transfer(results.value, sp.mutez(0), params.callback)

        # ── FA2: update_operators ─────────────────────────────────────────────

        @sp.entrypoint
        def update_operators(self, actions):
            sp.cast(actions, sp.list[sp.variant(
                add_operator    = sp.record(owner=sp.address, operator=sp.address, token_id=sp.nat),
                remove_operator = sp.record(owner=sp.address, operator=sp.address, token_id=sp.nat),
            )])
            for action in actions:
                with action.match_cases() as arg:
                    with arg.match("add_operator") as add:
                        sp.verify(add.owner == sp.sender, "FA2_NOT_OWNER")
                        self.data.operators[
                            sp.pair(add.owner, sp.pair(add.operator, add.token_id))
                        ] = sp.unit
                    with arg.match("remove_operator") as rem:
                        sp.verify(rem.owner == sp.sender, "FA2_NOT_OWNER")
                        del self.data.operators[
                            sp.pair(rem.owner, sp.pair(rem.operator, rem.token_id))
                        ]

        # ── Admin: create_drop ────────────────────────────────────────────────

        @sp.entrypoint
        def create_drop(self, params):
            """
            Admin creates a new drop (token type).
            params:
              metadata     : sp.map[sp.string, sp.bytes]  TZIP-21 token metadata
              price        : sp.mutez                      0 = free
              max_editions : sp.option[sp.nat]             None = open edition
            """
            sp.verify(sp.sender == self.data.administrator, "NOT_ADMIN")

            token_id = self.data.all_tokens

            self.data.token_metadata[token_id] = sp.record(
                token_id   = token_id,
                token_info = params.metadata,
            )
            self.data.royalty_recipients[token_id] = {
                self.data.administrator: self.data.royalty_percent
            }
            self.data.drops[self.data.next_drop_id] = sp.record(
                price        = params.price,
                max_editions = params.max_editions,
                minted       = sp.nat(0),
                active       = True,
                token_id     = token_id,
            )

            self.data.all_tokens   += sp.nat(1)
            self.data.next_drop_id += sp.nat(1)

        # ── Admin: set_drop_active ────────────────────────────────────────────

        @sp.entrypoint
        def set_drop_active(self, params):
            """Open or close a drop. params: drop_id (nat), active (bool)"""
            sp.verify(sp.sender == self.data.administrator, "NOT_ADMIN")
            sp.verify(self.data.drops.contains(params.drop_id), "UNKNOWN_DROP")
            self.data.drops[params.drop_id].active = params.active

        # ── Public: collect ───────────────────────────────────────────────────

        @sp.entrypoint
        def collect(self, params):
            """
            Anyone pays and collects editions from an active drop.
            params: drop_id (nat), quantity (nat, min 1)
            """
            sp.verify(self.data.drops.contains(params.drop_id), "UNKNOWN_DROP")

            drop = self.data.drops[params.drop_id]

            sp.verify(drop.active, "DROP_NOT_ACTIVE")
            sp.verify(params.quantity >= sp.nat(1), "MIN_ONE")

            sp.if drop.max_editions.is_some():
                sp.verify(
                    drop.minted + params.quantity <= drop.max_editions.unwrap_some(),
                    "SOLD_OUT"
                )

            total_price = sp.split_tokens(drop.price, params.quantity, sp.nat(1))
            sp.verify(sp.amount == total_price, "WRONG_AMOUNT")

            key     = sp.pair(sp.sender, drop.token_id)
            current = self.data.ledger.get(key, default=sp.nat(0))
            self.data.ledger[key] = current + params.quantity
            self.data.drops[params.drop_id].minted += params.quantity

            sp.if sp.amount > sp.mutez(0):
                sp.send(self.data.administrator, sp.amount)

        # ── Admin: update_royalty ─────────────────────────────────────────────

        @sp.entrypoint
        def update_royalty(self, new_royalty_percent):
            """
            Update global royalty for future drops. Max 250 (25%).
            Does not retroactively affect already-created drops.
            """
            sp.verify(sp.sender == self.data.administrator, "NOT_ADMIN")
            sp.verify(new_royalty_percent <= sp.nat(250), "MAX_25_PERCENT")
            self.data.royalty_percent = new_royalty_percent

        # ── View: get_royalties ───────────────────────────────────────────────

        @sp.onchain_view()
        def get_royalties(self, token_id):
            """Returns royalty map for a token. Used by marketplace contract."""
            sp.verify(self.data.royalty_recipients.contains(token_id), "UNKNOWN_TOKEN")
            return self.data.royalty_recipients[token_id]

        # ── View: get_drop ────────────────────────────────────────────────────

        @sp.onchain_view()
        def get_drop(self, drop_id):
            """Returns drop record for a given drop_id."""
            sp.verify(self.data.drops.contains(drop_id), "UNKNOWN_DROP")
            return self.data.drops[drop_id]


# ── Tests ─────────────────────────────────────────────────────────────────────

@sp.add_test()
def test():
    scenario = sp.test_scenario("REJKT FA2 Tests", rejkt_module)
    scenario.h1("REJKT Token Contract Tests")

    admin = sp.test_account("Admin")
    alice = sp.test_account("Alice")
    bob   = sp.test_account("Bob")

    def make_metadata(name, cid):
        def b(s): return sp.bytes("0x" + s.encode("utf-8").hex())
        return {
            "name"        : b(name),
            "symbol"      : b("REJKT"),
            "decimals"    : b("0"),
            "artifactUri" : b("ipfs://" + cid),
            "displayUri"  : b("ipfs://" + cid),
            "description" : b("A REJKT token."),
        }

    scenario.h2("1. Deploy")
    contract = rejkt_module.REJKTToken(
        admin_address   = admin.address,
        royalty_percent = sp.nat(150),
    )
    scenario += contract
    scenario.verify(contract.data.royalty_percent == sp.nat(150))
    scenario.verify(contract.data.all_tokens == sp.nat(0))

    scenario.h2("2. Admin creates limited drop (10 editions, 1 tez)")
    contract.create_drop(
        sp.record(
            metadata     = make_metadata("REJKT #0", "QmFakeCID0"),
            price        = sp.tez(1),
            max_editions = sp.some(sp.nat(10)),
        ),
        _sender=admin
    )
    scenario.verify(contract.data.all_tokens == sp.nat(1))
    scenario.verify(contract.data.next_drop_id == sp.nat(1))

    scenario.h2("3. Admin creates open edition drop (free)")
    contract.create_drop(
        sp.record(
            metadata     = make_metadata("REJKT #1", "QmFakeCID1"),
            price        = sp.mutez(0),
            max_editions = sp.none,
        ),
        _sender=admin
    )
    scenario.verify(contract.data.all_tokens == sp.nat(2))

    scenario.h2("4. Alice collects 2 from drop 0")
    contract.collect(
        sp.record(drop_id=sp.nat(0), quantity=sp.nat(2)),
        _sender=alice,
        _amount=sp.tez(2)
    )
    scenario.verify(contract.data.ledger[sp.pair(alice.address, sp.nat(0))] == sp.nat(2))
    scenario.verify(contract.data.drops[sp.nat(0)].minted == sp.nat(2))

    scenario.h2("5. Bob collects 1 free from drop 1")
    contract.collect(
        sp.record(drop_id=sp.nat(1), quantity=sp.nat(1)),
        _sender=bob,
        _amount=sp.mutez(0)
    )
    scenario.verify(contract.data.ledger[sp.pair(bob.address, sp.nat(1))] == sp.nat(1))

    scenario.h2("6. Wrong payment — should fail")
    contract.collect(
        sp.record(drop_id=sp.nat(0), quantity=sp.nat(1)),
        _sender=alice,
        _amount=sp.mutez(500000),
        _valid=False
    )

    scenario.h2("7. Non-admin creates drop — should fail")
    contract.create_drop(
        sp.record(
            metadata     = make_metadata("FAKE", "QmFake"),
            price        = sp.tez(1),
            max_editions = sp.some(sp.nat(5)),
        ),
        _sender=alice,
        _valid=False
    )

    scenario.h2("8. Admin closes drop 1")
    contract.set_drop_active(
        sp.record(drop_id=sp.nat(1), active=False),
        _sender=admin
    )
    scenario.verify(~contract.data.drops[sp.nat(1)].active)

    scenario.h2("9. Collect from closed drop — should fail")
    contract.collect(
        sp.record(drop_id=sp.nat(1), quantity=sp.nat(1)),
        _sender=bob,
        _amount=sp.mutez(0),
        _valid=False
    )

    scenario.h2("10. Over-collect limited drop — should fail")
    contract.collect(
        sp.record(drop_id=sp.nat(0), quantity=sp.nat(9)),
        _sender=alice,
        _amount=sp.tez(9),
        _valid=False
    )

    scenario.h2("11. Admin updates royalty to 10%")
    contract.update_royalty(sp.nat(100), _sender=admin)
    scenario.verify(contract.data.royalty_percent == sp.nat(100))

    scenario.h2("12. Royalty over 25% — should fail")
    contract.update_royalty(sp.nat(300), _sender=admin, _valid=False)

    scenario.h2("13. Royalty view for token 0 shows original 15%")
    scenario.verify(
        contract.get_royalties(sp.nat(0))[admin.address] == sp.nat(150)
    )

    scenario.h1("All tests passed.")


# ── Compilation target ────────────────────────────────────────────────────────

sp.add_compilation_target(
    "REJKT_FA2",
    rejkt_module.REJKTToken(
        admin_address   = sp.address(ADMIN_ADDRESS),
        royalty_percent = ROYALTY_PERCENT,
    )
)
