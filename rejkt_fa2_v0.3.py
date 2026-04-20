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
4. Replace CONTRACT_METADATA_URI with your IPFS metadata URI after uploading
5. Run tests, then deploy to Ghostnet first, then Mainnet

Upgrade path notes:
- royalty_recipients is stored as a map per token to support future splits
- Currently always set to {admin: royalty_percent} at mint time
- v2 will allow multiple recipients with split percentages

Mixin pattern (current SmartPy compiler):
- Inheritance order: Admin → mixins → Fungible (base class LAST)
- __init__ call order: mixins first, then Fungible.__init__, then Admin.__init__
- Use assert (not sp.verify) — matches the library's own style
- Token counter: self.data.next_token_id (set by CommonInterface.__init__ via Fungible)
- Ledger key: tuple (address, token_id) — Fungible uses sp.pair[sp.address, sp.nat]
- Supply: self.data.supply is sp.big_map[sp.nat, sp.nat] keyed by token_id
- MintFungible deliberately excluded — collect() handles all minting
"""

import smartpy as sp
from smartpy.templates import fa2_lib as fa2

# ── Constants (edit these before deploying) ───────────────────────────────────

ADMIN_ADDRESS         = "tz1YourWalletAddressHere"       # Your Tezos wallet
ROYALTY_PERCENT       = 150                               # 150 = 15% (out of 1000)
CONTRACT_METADATA_URI = "ipfs://YourContractMetadataCIDHere"

# ─────────────────────────────────────────────────────────────────────────────

main = fa2.main

@sp.module
def rejkt_module():
    import t
    import main

    # ── Storage types ─────────────────────────────────────────────────────────

    drop_type: type = sp.record(
        price        = sp.mutez,
        max_editions = sp.option[sp.nat],
        minted       = sp.nat,
        active       = sp.bool,
        token_id     = sp.nat,
    )

    royalty_map_type: type = sp.map[sp.address, sp.nat]

    # ── Contract ──────────────────────────────────────────────────────────────
    #
    # Inheritance order: Admin → OnchainviewBalanceOf → Fungible (base LAST)
    #
    # MintFungible is intentionally excluded. We need a paid, drop-based
    # collect entrypoint rather than the admin-only library mint. We write
    # directly to self.data.ledger, self.data.supply, and
    # self.data.token_metadata — all of which are initialised by Fungible.
    #
    # __init__ call order per the library source:
    #   OnchainviewBalanceOf (no storage) → Fungible → Admin

    class REJKTToken(
        main.Admin,
        main.OnchainviewBalanceOf,
        main.Fungible,                  # base class — always last
    ):
        def __init__(self, admin_address, contract_metadata, royalty_percent):
            # Fungible.__init__ sets up:
            #   self.data.ledger          : big_map[(address, nat), nat]
            #   self.data.supply          : big_map[nat, nat]
            #   self.data.token_metadata  : big_map[nat, record(...)]
            #   self.data.next_token_id   : nat  (= 0)
            #   self.data.metadata        : big_map[string, bytes]
            #   self.data.operators       : big_map[operator_permission, unit]
            main.OnchainviewBalanceOf.__init__(self)
            main.Fungible.__init__(self, contract_metadata, {}, [])
            main.Admin.__init__(self, admin_address)

            # REJKT-specific storage
            self.data.royalty_percent = sp.cast(royalty_percent, sp.nat)
            self.data.next_drop_id    = sp.nat(0)

            self.data.drops = sp.cast(
                sp.big_map(),
                sp.big_map[sp.nat, drop_type]
            )

            self.data.royalty_recipients = sp.cast(
                sp.big_map(),
                sp.big_map[sp.nat, royalty_map_type]
            )

        # ── Admin: Create a drop ─────────────────────────────────────────

        @sp.entrypoint
        def create_drop(self, params):
            """
            Admin creates a new drop (token type).

            params:
              - metadata     : sp.map[sp.string, sp.bytes]  (TZIP-21 token metadata)
              - price        : sp.mutez                      (0 = free)
              - max_editions : sp.option[sp.nat]             (None = open edition)
            """
            assert self.is_administrator_(), "NOT_ADMIN"

            # next_token_id is the auto-increment counter from CommonInterface.
            token_id = self.data.next_token_id

            self.data.token_metadata[token_id] = sp.record(
                token_id   = token_id,
                token_info = params.metadata,
            )

            # Initialise supply counter for this token (required by Fungible)
            self.data.supply[token_id] = sp.nat(0)

            self.data.next_token_id += 1

            # Store royalty recipients for this token (single entry for v1)
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

            self.data.next_drop_id += 1

        # ── Admin: Open / close a drop ───────────────────────────────────

        @sp.entrypoint
        def set_drop_active(self, params):
            """
            Admin opens or closes a drop.

            params:
              - drop_id : sp.nat
              - active  : sp.bool
            """
            assert self.is_administrator_(), "NOT_ADMIN"
            assert self.data.drops.contains(params.drop_id), "UNKNOWN_DROP"
            self.data.drops[params.drop_id].active = params.active

        # ── Public: Collect (mint) from a drop ───────────────────────────

        @sp.entrypoint
        def collect(self, params):
            """
            Anyone can collect an edition from an active drop by paying the price.

            params:
              - drop_id  : sp.nat
              - quantity : sp.nat  (how many editions to collect, min 1)
            """
            assert self.data.drops.contains(params.drop_id), "UNKNOWN_DROP"

            drop = self.data.drops[params.drop_id]

            assert drop.active,                  "DROP_NOT_ACTIVE"
            assert params.quantity >= sp.nat(1), "MIN_ONE"

            # Edition limit check
            match drop.max_editions:
                case Some(max_ed):
                    assert drop.minted + params.quantity <= max_ed, "SOLD_OUT"
                case None:
                    pass

            # Payment check
            total_price = sp.split_tokens(drop.price, params.quantity, sp.nat(1))
            assert sp.amount == total_price, "WRONG_AMOUNT"

            token_id = drop.token_id

            # Credit collector.
            # Fungible ledger key is (address, token_id) — a sp.pair.
            key = (sp.sender, token_id)
            self.data.ledger[key] = (
                self.data.ledger.get(key, default=sp.nat(0)) + params.quantity
            )

            # Keep the Fungible supply counter consistent
            self.data.supply[token_id] = (
                self.data.supply.get(token_id, default=sp.nat(0)) + params.quantity
            )

            self.data.drops[params.drop_id].minted += params.quantity

            # Forward payment to admin
            if sp.amount > sp.mutez(0):
                sp.send(self.data.administrator, sp.amount)

        # ── Admin: Update royalty percent ────────────────────────────────

        @sp.entrypoint
        def update_royalty(self, new_royalty_percent):
            """
            Admin updates the global royalty percent for future drops.
            Does NOT retroactively change already-created token entries.

            new_royalty_percent: sp.nat (e.g. 150 = 15%, max 250 = 25%)
            """
            assert self.is_administrator_(),                "NOT_ADMIN"
            assert new_royalty_percent <= sp.nat(250), "MAX_ROYALTY_25_PERCENT"
            self.data.royalty_percent = new_royalty_percent

        # ── View: Get royalty info for a token ───────────────────────────

        @sp.onchain_view()
        def get_royalties(self, token_id):
            """
            Returns the royalty map for a given token_id.
            Used by marketplace contracts to distribute royalties on sale.
            """
            assert self.data.royalty_recipients.contains(token_id), "UNKNOWN_TOKEN"
            return self.data.royalty_recipients[token_id]

        # ── View: Get drop info ───────────────────────────────────────────

        @sp.onchain_view()
        def get_drop(self, drop_id):
            """Returns full drop record for a given drop_id."""
            assert self.data.drops.contains(drop_id), "UNKNOWN_DROP"
            return self.data.drops[drop_id]


# ── Tests ─────────────────────────────────────────────────────────────────────

@sp.add_test()
def test():
    scenario = sp.test_scenario("REJKT FA2 Tests", rejkt_module)
    scenario.h1("REJKT Token Contract Tests")

    admin = sp.test_account("Admin")
    alice = sp.test_account("Alice")
    bob   = sp.test_account("Bob")

    def make_token_metadata(name, symbol, ipfs_cid):
        def b(s): return sp.bytes("0x" + s.encode("utf-8").hex())
        return {
            "name"        : b(name),
            "symbol"      : b(symbol),
            "decimals"    : b("0"),
            "artifactUri" : b("ipfs://" + ipfs_cid),
            "displayUri"  : b("ipfs://" + ipfs_cid),
            "thumbnailUri": b("ipfs://" + ipfs_cid),
            "description" : b("A REJKT token."),
            "minter"      : b(admin.address.__str__()),
        }

    # ── Deploy ────────────────────────────────────────────────────────────────

    scenario.h2("1. Deploy")
    contract = rejkt_module.REJKTToken(
        admin_address     = admin.address,
        contract_metadata = sp.big_map({
            "": sp.bytes("0x" + CONTRACT_METADATA_URI.encode("utf-8").hex())
        }),
        royalty_percent   = ROYALTY_PERCENT,
    )
    scenario += contract
    scenario.verify(contract.data.royalty_percent  == ROYALTY_PERCENT)
    scenario.verify(contract.data.next_token_id    == sp.nat(0))
    scenario.verify(contract.data.next_drop_id     == sp.nat(0))

    # ── Create a limited drop ─────────────────────────────────────────────────

    scenario.h2("2. Admin creates a limited drop (10 editions, 1 tez each)")
    contract.create_drop(
        sp.record(
            metadata     = make_token_metadata("REJKT #0", "REJKT", "QmFakeCID0"),
            price        = sp.tez(1),
            max_editions = sp.some(sp.nat(10)),
        ),
        _sender = admin
    )
    scenario.verify(contract.data.next_token_id == sp.nat(1))
    scenario.verify(contract.data.next_drop_id  == sp.nat(1))
    scenario.verify(contract.data.token_metadata.contains(sp.nat(0)))
    scenario.verify(contract.data.supply[sp.nat(0)] == sp.nat(0))

    # ── Create an open edition drop ───────────────────────────────────────────

    scenario.h2("3. Admin creates an open edition drop (free)")
    contract.create_drop(
        sp.record(
            metadata     = make_token_metadata("REJKT #1", "REJKT", "QmFakeCID1"),
            price        = sp.mutez(0),
            max_editions = sp.none,
        ),
        _sender = admin
    )
    scenario.verify(contract.data.next_token_id == sp.nat(2))
    scenario.verify(contract.data.next_drop_id  == sp.nat(2))

    # ── Collect from limited drop ─────────────────────────────────────────────

    scenario.h2("4. Alice collects 2 editions from drop 0")
    contract.collect(
        sp.record(drop_id=sp.nat(0), quantity=sp.nat(2)),
        _sender = alice,
        _amount = sp.tez(2)
    )
    scenario.verify(
        contract.data.ledger[sp.pair(alice.address, sp.nat(0))] == sp.nat(2)
    )
    scenario.verify(contract.data.drops[sp.nat(0)].minted == sp.nat(2))
    scenario.verify(contract.data.supply[sp.nat(0)]       == sp.nat(2))

    # ── Collect from open edition ─────────────────────────────────────────────

    scenario.h2("5. Bob collects 1 free edition from drop 1")
    contract.collect(
        sp.record(drop_id=sp.nat(1), quantity=sp.nat(1)),
        _sender = bob,
        _amount = sp.mutez(0)
    )
    scenario.verify(
        contract.data.ledger[sp.pair(bob.address, sp.nat(1))] == sp.nat(1)
    )
    scenario.verify(contract.data.supply[sp.nat(1)] == sp.nat(1))

    # ── Wrong payment rejected ────────────────────────────────────────────────

    scenario.h2("6. Alice tries to underpay — should fail")
    contract.collect(
        sp.record(drop_id=sp.nat(0), quantity=sp.nat(1)),
        _sender = alice,
        _amount = sp.mutez(500000),
        _valid  = False
    )

    # ── Non-admin cannot create drop ──────────────────────────────────────────

    scenario.h2("7. Alice tries to create a drop — should fail")
    contract.create_drop(
        sp.record(
            metadata     = make_token_metadata("FAKE", "FAKE", "QmFake"),
            price        = sp.tez(1),
            max_editions = sp.some(sp.nat(5)),
        ),
        _sender = alice,
        _valid  = False
    )

    # ── Close drop ────────────────────────────────────────────────────────────

    scenario.h2("8. Admin closes drop 1")
    contract.set_drop_active(
        sp.record(drop_id=sp.nat(1), active=False),
        _sender = admin
    )
    scenario.verify(~contract.data.drops[sp.nat(1)].active)

    # ── Collect from closed drop fails ────────────────────────────────────────

    scenario.h2("9. Bob tries to collect from closed drop — should fail")
    contract.collect(
        sp.record(drop_id=sp.nat(1), quantity=sp.nat(1)),
        _sender = bob,
        _amount = sp.mutez(0),
        _valid  = False
    )

    # ── Sold out ──────────────────────────────────────────────────────────────

    scenario.h2("10. Alice tries to over-collect limited drop — should fail")
    contract.collect(
        sp.record(drop_id=sp.nat(0), quantity=sp.nat(9)),  # 2 already minted, only 8 left
        _sender = alice,
        _amount = sp.tez(9),
        _valid  = False
    )

    # ── Update royalty ────────────────────────────────────────────────────────

    scenario.h2("11. Admin updates royalty to 10%")
    contract.update_royalty(sp.nat(100), _sender=admin)
    scenario.verify(contract.data.royalty_percent == sp.nat(100))

    # ── Royalty over 25% rejected ─────────────────────────────────────────────

    scenario.h2("12. Admin tries to set royalty over 25% — should fail")
    contract.update_royalty(sp.nat(300), _sender=admin, _valid=False)

    # ── Royalty view ──────────────────────────────────────────────────────────

    scenario.h2("13. Royalty view for token 0 shows original 15%")
    scenario.verify(
        contract.get_royalties(sp.nat(0))[admin.address] == sp.nat(ROYALTY_PERCENT)
    )

    # ── FA2 transfer (from the library's Fungible base class) ─────────────────

    scenario.h2("14. Alice transfers 1 edition of token 0 to Bob via FA2 transfer")
    contract.transfer(
        [sp.record(
            from_ = alice.address,
            txs   = [sp.record(to_=bob.address, token_id=sp.nat(0), amount=sp.nat(1))]
        )],
        _sender = alice
    )
    scenario.verify(
        contract.data.ledger[sp.pair(alice.address, sp.nat(0))] == sp.nat(1)
    )
    scenario.verify(
        contract.data.ledger[sp.pair(bob.address, sp.nat(0))] == sp.nat(1)
    )

    scenario.h1("All tests passed.")


# ── Compilation target ────────────────────────────────────────────────────────

sp.add_compilation_target(
    "REJKT_FA2",
    rejkt_module.REJKTToken(
        admin_address     = sp.address(ADMIN_ADDRESS),
        contract_metadata = sp.big_map({
            "": sp.bytes("0x" + CONTRACT_METADATA_URI.encode("utf-8").hex())
        }),
        royalty_percent   = sp.nat(ROYALTY_PERCENT),
    )
)
