from flux_gallery.resolution import compute_target_resolution


def test_screen_f_1800x1400_rounds_up_to_nearest_16():
    assert compute_target_resolution(1800, 1400) == (1808, 1408)


def test_screens_b_and_c_1200x600_round_height_up_to_nearest_16():
    assert compute_target_resolution(1200, 600) == (1200, 608)


def test_screens_d_a_e_1600x400_already_multiples_of_16_unchanged():
    assert compute_target_resolution(1600, 400) == (1600, 400)
