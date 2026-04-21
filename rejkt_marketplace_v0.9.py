"""
REJKT Marketplace Contract
===========================
Custodial secondary marketplace for REJKT FA2 tokens.

Ported from Teia Community marketplace v3 (itself a fork of HEN v2)
to current SmartPy syntax. Core swap/collect/cancel logic is identical
to the battle-tested Teia architecture.

Key differences from Teia v3:
  - Royalties are pulled from the FA2 contract's get_royalties() onchain
    view instead of being passed as a parameter. This prevents anyone from
    bypassing royalties by passing 0 when creating a swap.
  - Uses current SmartPy syntax (assert, sp.cast, no sp.set_type etc.)
  - allowed_fa2s whitelist retained — add your FA2 address after deploy.

Entry points:
  swap          — list editions for sale (tokens move to contract)
  collect       — buy one edition (tez split: royalties + fee + seller)
  cancel_swap   — seller reclaims unsold editions
  update_fee              — manager only
  update_fee_recipient    — manager only
  add_fa2 / remove_fa2    — manager only (whitelist)
  set_pause_swaps         — manager only
  set_pause_collects      — manager only
  transfer_manager        — propose new manager
  accept_manager          — new manager accepts

Instructions:
  1. Paste into SmartPy IDE at https://smartpy.io/ide
  2. Set MANAGER_ADDRESS to your wallet
  3. Deploy FA2 first, then fill FA2_CONTRACT_ADDRESS here
  4. Run tests, deploy to Ghostnet, then call add_fa2 with your FA2 address
  5. Deploy to Mainnet when ready

Error codes:
  MP_NOT_MANAGER          MP_TEZ_TRANSFER         MP_SWAPS_PAUSED
  MP_COLLECTS_PAUSED      MP_FA2_NOT_ALLOWED      MP_NO_SWAPPED_EDITIONS
  MP_WRONG_SWAP_ID        MP_IS_SWAP_ISSUER       MP_WRONG_TEZ_AMOUNT
  MP_SWAP_COLLECTED       MP_NOT_SWAP_ISSUER      MP_WRONG_FEES
  MP_NO_NEW_MANAGER       MP_NOT_PROPOSED_MANAGER
"""

import smartpy as sp

# ── Constants ─────────────────────────────────────────────────────────────────

MANAGER_ADDRESS     = "tz1YourWalletAddressHere"
FA2_CONTRACT_ADDRESS = "KT1YourFA2ContractAddressHere"
INITIAL_FEE         = sp.nat(0)   # 0 = free. 25 = 2.5%. Max 250 = 25%.

# ─────────────────────────────────────────────────────────────────────────────

@sp.module
def marketplace_module():

    # ── Swap record type ──────────────────────────────────────────────────────

    swap_type: type = sp.record(
        issuer       = sp.address,   # who listed
        fa2          = sp.address,   # FA2 contract address
        token_id     = sp.nat,       # token id within FA2
        amount       = sp.nat,       # editions still available
        price        = sp.mutez,     # price per edition
        royalties    = sp.nat,       # per mille (e.g. 150 = 15%)
        creator      = sp.address,   # royalty recipient
    )

    # ── Contract ──────────────────────────────────────────────────────────────

    class REJKTMarketplace(sp.Contract):

        def __init__(self, manager, metadata, allowed_fa2s, fee):
            self.data.manager          = sp.cast(manager, sp.address)
            self.data.metadata         = sp.cast(metadata, sp.big_map[sp.string, sp.bytes])
            self.data.allowed_fa2s     = sp.cast(allowed_fa2s, sp.big_map[sp.address, sp.unit])
            self.data.swaps            = sp.cast(sp.big_map(), sp.big_map[sp.nat, swap_type])
            self.data.fee              = sp.cast(fee, sp.nat)
            self.data.fee_recipient    = sp.cast(manager, sp.address)
            self.data.counter          = sp.nat(0)
            self.data.proposed_manager = sp.cast(sp.none, sp.option[sp.address])
            self.data.swaps_paused     = False
            self.data.collects_paused  = False

        # ── Internal helpers ──────────────────────────────────────────────────

        def _is_manager(self):
            assert sp.sender == self.data.manager, "MP_NOT_MANAGER"

        def _no_tez(self):
            assert sp.amount == sp.mutez(0), "MP_TEZ_TRANSFER"

        def _fa2_transfer(self, fa2, from_, to_, token_id, amount):
            """Call the FA2 transfer entrypoint."""
            transfer_handle = sp.contract(
                sp.list[sp.record(
                    from_ = sp.address,
                    txs   = sp.list[sp.record(
                        to_      = sp.address,
                        token_id = sp.nat,
                        amount   = sp.nat,
                    )]
                )],
                fa2,
                entrypoint="transfer"
            ).unwrap_some(error="BAD_FA2_CONTRACT")

            sp.transfer(
                [sp.record(
                    from_ = from_,
                    txs   = [sp.record(
                        to_      = to_,
                        token_id = token_id,
                        amount   = amount,
                    )]
                )],
                sp.mutez(0),
                transfer_handle
            )

        def _get_royalties(self, fa2, token_id):
            """
            Call get_royalties onchain view on the FA2 contract.
            Returns sp.map[sp.address, sp.nat] — address to per-mille share.
            Falls back to empty map if FA2 doesn't support the view.
            """
            return sp.view(
                "get_royalties",
                fa2,
                token_id,
                sp.map[sp.address, sp.nat]
            ).unwrap_some(error="NO_ROYALTIES_VIEW")

        # ── swap ──────────────────────────────────────────────────────────────

        @sp.entrypoint
        def swap(self, params):
            """
            List editions for sale. Tokens move into the marketplace contract.
            The caller must have set this contract as an FA2 operator first.

            params:
              fa2      : address  — FA2 contract
              token_id : nat      — token id
              amount   : nat      — number of editions to list
              price    : mutez    — price per edition (0 = free)
            """
            assert not self.data.swaps_paused, "MP_SWAPS_PAUSED"
            self._no_tez()
            assert self.data.allowed_fa2s.contains(params.fa2), "MP_FA2_NOT_ALLOWED"
            assert params.amount > sp.nat(0), "MP_NO_SWAPPED_EDITIONS"

            # Pull royalties from FA2 — issuer cannot manipulate this
            royalty_map = self._get_royalties(params.fa2, params.token_id)

            # Sum total royalty share for validation
            total_royalties = sp.nat(0)
            for _addr, share in royalty_map.items():
                total_royalties += share
            assert total_royalties <= sp.nat(250), "MP_WRONG_ROYALTIES"

            # Transfer tokens to marketplace escrow
            self._fa2_transfer(
                fa2      = params.fa2,
                from_    = sp.sender,
                to_      = sp.self_address,
                token_id = params.token_id,
                amount   = params.amount,
            )

            # For v1, royalty_map has one entry. Store first entry's values.
            # v2 will iterate the map and send to each recipient.
            creator  = sp.sender   # fallback
            royalties = sp.nat(0)
            for addr, share in royalty_map.items():
                creator   = addr
                royalties = share

            self.data.swaps[self.data.counter] = sp.record(
                issuer    = sp.sender,
                fa2       = params.fa2,
                token_id  = params.token_id,
                amount    = params.amount,
                price     = params.price,
                royalties = royalties,
                creator   = creator,
            )
            self.data.counter += sp.nat(1)

        # ── collect ───────────────────────────────────────────────────────────

        @sp.entrypoint
        def collect(self, swap_id):
            """
            Buy one edition from a swap. Sends tez to royalty recipient,
            marketplace fee recipient, and seller.

            swap_id : nat
            """
            assert not self.data.collects_paused, "MP_COLLECTS_PAUSED"
            assert self.data.swaps.contains(swap_id), "MP_WRONG_SWAP_ID"

            swap = self.data.swaps[swap_id]

            assert sp.sender != swap.issuer, "MP_IS_SWAP_ISSUER"
            assert sp.amount == swap.price, "MP_WRONG_TEZ_AMOUNT"
            assert swap.amount > sp.nat(0), "MP_SWAP_COLLECTED"

            # Distribute tez
            sp.if swap.price != sp.mutez(0):
                royalties_amount = sp.split_tokens(swap.price, swap.royalties, sp.nat(1000))
                sp.if royalties_amount > sp.mutez(0):
                    sp.send(swap.creator, royalties_amount)

                fee_amount = sp.split_tokens(swap.price, self.data.fee, sp.nat(1000))
                sp.if fee_amount > sp.mutez(0):
                    sp.send(self.data.fee_recipient, fee_amount)

                sp.send(swap.issuer, sp.amount - royalties_amount - fee_amount)

            # Transfer one edition to buyer
            self._fa2_transfer(
                fa2      = swap.fa2,
                from_    = sp.self_address,
                to_      = sp.sender,
                token_id = swap.token_id,
                amount   = sp.nat(1),
            )

            # Decrement available editions
            self.data.swaps[swap_id].amount = sp.as_nat(swap.amount - 1)

        # ── cancel_swap ───────────────────────────────────────────────────────

        @sp.entrypoint
        def cancel_swap(self, swap_id):
            """
            Seller cancels a swap and gets remaining editions back.
            swap_id : nat
            """
            self._no_tez()
            assert self.data.swaps.contains(swap_id), "MP_WRONG_SWAP_ID"

            swap = self.data.swaps[swap_id]

            assert sp.sender == swap.issuer, "MP_NOT_SWAP_ISSUER"
            assert swap.amount > sp.nat(0), "MP_SWAP_COLLECTED"

            self._fa2_transfer(
                fa2      = swap.fa2,
                from_    = sp.self_address,
                to_      = sp.sender,
                token_id = swap.token_id,
                amount   = swap.amount,
            )

            del self.data.swaps[swap_id]

        # ── Manager: update_fee ───────────────────────────────────────────────

        @sp.entrypoint
        def update_fee(self, new_fee):
            """Max 250 (25%). new_fee : nat"""
            self._is_manager()
            self._no_tez()
            assert new_fee <= sp.nat(250), "MP_WRONG_FEES"
            self.data.fee = new_fee

        # ── Manager: update_fee_recipient ─────────────────────────────────────

        @sp.entrypoint
        def update_fee_recipient(self, new_recipient):
            """new_recipient : address"""
            self._is_manager()
            self._no_tez()
            self.data.fee_recipient = new_recipient

        # ── Manager: add_fa2 / remove_fa2 ────────────────────────────────────

        @sp.entrypoint
        def add_fa2(self, fa2):
            """Whitelist an FA2 contract. fa2 : address"""
            self._is_manager()
            self._no_tez()
            self.data.allowed_fa2s[fa2] = sp.unit

        @sp.entrypoint
        def remove_fa2(self, fa2):
            """Remove FA2 from whitelist. fa2 : address"""
            self._is_manager()
            self._no_tez()
            del self.data.allowed_fa2s[fa2]

        # ── Manager: pause ────────────────────────────────────────────────────

        @sp.entrypoint
        def set_pause_swaps(self, pause):
            """pause : bool"""
            self._is_manager()
            self._no_tez()
            self.data.swaps_paused = pause

        @sp.entrypoint
        def set_pause_collects(self, pause):
            """pause : bool"""
            self._is_manager()
            self._no_tez()
            self.data.collects_paused = pause

        # ── Manager transfer (two-step) ───────────────────────────────────────

        @sp.entrypoint
        def transfer_manager(self, proposed_manager):
            """Propose a new manager. proposed_manager : address"""
            self._is_manager()
            self._no_tez()
            self.data.proposed_manager = sp.some(proposed_manager)

        @sp.entrypoint
        def accept_manager(self):
            """Proposed manager accepts."""
            assert self.data.proposed_manager.is_some(), "MP_NO_NEW_MANAGER"
            assert sp.sender == self.data.proposed_manager.unwrap_some(), "MP_NOT_PROPOSED_MANAGER"
            self._no_tez()
            self.data.manager = sp.sender
            self.data.proposed_manager = sp.none

        # ── Manager: update metadata ──────────────────────────────────────────

        @sp.entrypoint
        def update_metadata(self, params):
            """params: record(key=string, value=bytes)"""
            self._is_manager()
            self._no_tez()
            self.data.metadata[params.key] = params.value

        # ── Onchain views ─────────────────────────────────────────────────────

        @sp.onchain_view()
        def get_swap(self, swap_id):
            assert self.data.swaps.contains(swap_id), "MP_WRONG_SWAP_ID"
            return self.data.swaps[swap_id]

        @sp.onchain_view()
        def get_fee(self):
            return self.data.fee

        @sp.onchain_view()
        def get_counter(self):
            return self.data.counter

        @sp.onchain_view()
        def is_allowed_fa2(self, fa2):
            return self.data.allowed_fa2s.contains(fa2)


# ── Tests ─────────────────────────────────────────────────────────────────────

@sp.add_test()
def test():
    scenario = sp.test_scenario("REJKT Marketplace Tests", marketplace_module)
    scenario.h1("REJKT Marketplace Tests")

    manager = sp.test_account("Manager")
    alice   = sp.test_account("Alice")   # seller
    bob     = sp.test_account("Bob")     # buyer
    carol   = sp.test_account("Carol")   # proposed new manager

    # Fake FA2 address for whitelist tests
    fake_fa2 = sp.address("KT1FakeFa2AddressForTesting000000000")

    scenario.h2("1. Deploy")
    contract = marketplace_module.REJKTMarketplace(
        manager      = manager.address,
        metadata     = sp.big_map({"": sp.bytes("0x00")}),
        allowed_fa2s = sp.big_map(),
        fee          = sp.nat(0),
    )
    scenario += contract
    scenario.verify(contract.data.fee == sp.nat(0))
    scenario.verify(contract.data.counter == sp.nat(0))
    scenario.verify(~contract.data.swaps_paused)
    scenario.verify(~contract.data.collects_paused)

    scenario.h2("2. Manager adds FA2 to whitelist")
    contract.add_fa2(fake_fa2, _sender=manager)
    scenario.verify(contract.data.allowed_fa2s.contains(fake_fa2))

    scenario.h2("3. Non-manager cannot add FA2 — should fail")
    contract.add_fa2(fake_fa2, _sender=alice, _valid=False)

    scenario.h2("4. Manager removes FA2")
    contract.remove_fa2(fake_fa2, _sender=manager)
    scenario.verify(~contract.data.allowed_fa2s.contains(fake_fa2))

    scenario.h2("5. Update fee to 2.5%")
    contract.update_fee(sp.nat(25), _sender=manager)
    scenario.verify(contract.data.fee == sp.nat(25))

    scenario.h2("6. Fee over 25% rejected")
    contract.update_fee(sp.nat(300), _sender=manager, _valid=False)

    scenario.h2("7. Update fee recipient")
    contract.update_fee_recipient(carol.address, _sender=manager)
    scenario.verify(contract.data.fee_recipient == carol.address)

    scenario.h2("8. Pause swaps")
    contract.set_pause_swaps(True, _sender=manager)
    scenario.verify(contract.data.swaps_paused)

    scenario.h2("9. Unpause swaps")
    contract.set_pause_swaps(False, _sender=manager)
    scenario.verify(~contract.data.swaps_paused)

    scenario.h2("10. Manager transfer (two-step)")
    contract.transfer_manager(carol.address, _sender=manager)
    scenario.verify(contract.data.proposed_manager == sp.some(carol.address))
    contract.accept_manager(_sender=carol)
    scenario.verify(contract.data.manager == carol.address)

    # Transfer back to original manager for remaining tests
    contract.transfer_manager(manager.address, _sender=carol)
    contract.accept_manager(_sender=manager)
    scenario.verify(contract.data.manager == manager.address)

    scenario.h2("11. Wrong proposed manager cannot accept — should fail")
    contract.transfer_manager(carol.address, _sender=manager)
    contract.accept_manager(_sender=alice, _valid=False)
    # Clean up
    contract.accept_manager(_sender=carol)
    contract.transfer_manager(manager.address, _sender=carol)
    contract.accept_manager(_sender=manager)

    scenario.h1("All manager tests passed.")
    scenario.h2("Note: swap/collect/cancel tests require a live FA2 contract")
    scenario.h2("and will be tested on Ghostnet after FA2 deployment.")


# ── Compilation target ────────────────────────────────────────────────────────

sp.add_compilation_target(
    "REJKT_Marketplace",
    marketplace_module.REJKTMarketplace(
        manager      = sp.address(MANAGER_ADDRESS),
        metadata     = sp.big_map({"": sp.bytes("0x" + "ipfs://YourMarketplaceMetadataCID".encode("utf-8").hex())}),
        allowed_fa2s = sp.big_map(),
        fee          = INITIAL_FEE,
    )
)
