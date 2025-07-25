from __future__ import annotations

from chia_rs import PlotSize
from chia_rs.sized_ints import uint8, uint16, uint32, uint64, uint128
from pytest import raises

from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.consensus.pos_quality import _expected_plot_size
from chia.consensus.pot_iterations import (
    calculate_ip_iters,
    calculate_iterations_quality,
    calculate_phase_out,
    calculate_sp_interval_iters,
    calculate_sp_iters,
    is_overflow_block,
)
from chia.util.hash import std_hash

test_constants = DEFAULT_CONSTANTS.replace(
    NUM_SPS_SUB_SLOT=uint8(32), SUB_SLOT_TIME_TARGET=uint16(300), HARD_FORK2_HEIGHT=uint32(100000000)
)


class TestPotIterations:
    def test_is_overflow_block(self):
        assert not is_overflow_block(test_constants, uint8(27))
        assert not is_overflow_block(test_constants, uint8(28))
        assert is_overflow_block(test_constants, uint8(29))
        assert is_overflow_block(test_constants, uint8(30))
        assert is_overflow_block(test_constants, uint8(31))
        with raises(ValueError):
            assert is_overflow_block(test_constants, uint8(32))

    def test_calculate_sp_iters(self):
        ssi: uint64 = uint64(100001 * 64 * 4)
        with raises(ValueError):
            calculate_sp_iters(test_constants, ssi, uint8(32))
        calculate_sp_iters(test_constants, ssi, uint8(31))

    def test_calculate_ip_iters(self):
        ssi: uint64 = uint64(100001 * 64 * 4)
        sp_interval_iters = ssi // test_constants.NUM_SPS_SUB_SLOT

        with raises(ValueError):
            # Invalid signage point index
            calculate_ip_iters(test_constants, ssi, uint8(123), uint64(100000))

        sp_iters = sp_interval_iters * 13

        with raises(ValueError):
            # required_iters too high
            calculate_ip_iters(test_constants, ssi, sp_interval_iters, sp_interval_iters)

        with raises(ValueError):
            # required_iters too high
            calculate_ip_iters(test_constants, ssi, sp_interval_iters, sp_interval_iters * 12)

        with raises(ValueError):
            # required_iters too low (0)
            calculate_ip_iters(test_constants, ssi, sp_interval_iters, uint64(0))

        required_iters = sp_interval_iters - 1
        ip_iters = calculate_ip_iters(test_constants, ssi, uint8(13), required_iters)
        assert ip_iters == sp_iters + test_constants.NUM_SP_INTERVALS_EXTRA * sp_interval_iters + required_iters

        required_iters = uint64(1)
        ip_iters = calculate_ip_iters(test_constants, ssi, uint8(13), required_iters)
        assert ip_iters == sp_iters + test_constants.NUM_SP_INTERVALS_EXTRA * sp_interval_iters + required_iters

        required_iters = uint64(ssi * 4 / 300)
        ip_iters = calculate_ip_iters(test_constants, ssi, uint8(13), required_iters)
        assert ip_iters == sp_iters + test_constants.NUM_SP_INTERVALS_EXTRA * sp_interval_iters + required_iters
        assert sp_iters < ip_iters

        # Overflow
        sp_iters = sp_interval_iters * (test_constants.NUM_SPS_SUB_SLOT - 1)
        ip_iters = calculate_ip_iters(
            test_constants,
            ssi,
            uint8(test_constants.NUM_SPS_SUB_SLOT - 1),
            required_iters,
        )
        assert ip_iters == (sp_iters + test_constants.NUM_SP_INTERVALS_EXTRA * sp_interval_iters + required_iters) % ssi
        assert sp_iters > ip_iters

    def test_win_percentage(self):
        """
        Tests that the percentage of blocks won is proportional to the space of each farmer,
        with the assumption that all farmers have access to the same VDF speed.
        """
        farmer_ks = {
            uint8(32): 100,
            uint8(33): 100,
            uint8(34): 100,
            uint8(35): 100,
            uint8(36): 100,
        }
        farmer_space = {k: _expected_plot_size(uint8(k)) * count for k, count in farmer_ks.items()}
        total_space = sum(farmer_space.values())
        percentage_space = {k: float(sp / total_space) for k, sp in farmer_space.items()}
        wins = {k: 0 for k in farmer_ks.keys()}
        total_slots = 50
        num_sps = 16
        sp_interval_iters = uint64(100000000 // 32)
        difficulty = uint64(500000000000)
        constants = test_constants.replace(DIFFICULTY_CONSTANT_FACTOR=uint128(2**25))
        for slot_index in range(total_slots):
            total_wins_in_slot = 0
            for sp_index in range(num_sps):
                sp_hash = std_hash(slot_index.to_bytes(4, "big") + sp_index.to_bytes(4, "big"))
                for k, count in farmer_ks.items():
                    for farmer_index in range(count):
                        quality = std_hash(slot_index.to_bytes(4, "big") + k.to_bytes(1, "big") + bytes(farmer_index))
                        # TODO: todo_v2_plots
                        required_iters = calculate_iterations_quality(
                            constants, quality, PlotSize.make_v1(k), difficulty, sp_hash, uint64(100000000), uint32(0)
                        )
                        if required_iters < sp_interval_iters:
                            wins[k] += 1
                            total_wins_in_slot += 1

        win_percentage = {k: wins[k] / sum(wins.values()) for k in farmer_ks.keys()}
        for k in farmer_ks.keys():
            # Win rate is proportional to percentage of space
            assert abs(win_percentage[k] - percentage_space[k]) < 0.01

    def test_calculate_phase_out(self):
        constants = test_constants
        sub_slot_iters = uint64(100000000000)
        sp_interval = calculate_sp_interval_iters(constants, sub_slot_iters)
        # Before or at HARD_FORK2_HEIGHT, should return 0
        assert calculate_phase_out(constants, sub_slot_iters, constants.HARD_FORK2_HEIGHT - 1) == 0
        assert calculate_phase_out(constants, sub_slot_iters, constants.HARD_FORK2_HEIGHT) == 0
        # after HARD_FORK2_HEIGHT, should return value = delta/phase_out_period * sp_interval
        assert (
            calculate_phase_out(constants, sub_slot_iters, constants.HARD_FORK2_HEIGHT + 1)
            == sp_interval // constants.PLOT_V1_PHASE_OUT
        )
        assert (
            calculate_phase_out(
                constants, sub_slot_iters, constants.HARD_FORK2_HEIGHT + constants.PLOT_V1_PHASE_OUT // 2
            )
            == sp_interval // 2
        )
        assert (
            calculate_phase_out(constants, sub_slot_iters, constants.HARD_FORK2_HEIGHT + constants.PLOT_V1_PHASE_OUT)
            == sp_interval
        )

        # Test with maximum uint32 height to ensure no overflow
        max_uint32_height = uint32(0xFFFFFFFF)
        result_max_height = calculate_phase_out(constants, sub_slot_iters, max_uint32_height)
        assert result_max_height == sp_interval  # Should cap at sp_interval
