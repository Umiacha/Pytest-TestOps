import pytest


def test_example():
    """Just for check plugin life-cycle. Do with pytest -s key"""
    assert 1 + 1 == 2


# @pytest.mark.parametrize('nums', ['1', '10', '100'])
# def test_parametrize(nums):
#     assert nums == nums