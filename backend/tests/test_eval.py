import importlib.util
import os

import pytest

_spec = importlib.util.spec_from_file_location(
    "run_eval", os.path.join(os.path.dirname(os.path.dirname(__file__)), "eval", "run_eval.py"))
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


def test_spearman_perfect_and_inverse():
    assert run_eval.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert run_eval.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_handles_ties_and_degenerate_input():
    assert run_eval._ranks([5, 1, 5]) == [2.5, 1.0, 2.5]
    assert run_eval.spearman([1, 1, 1], [1, 2, 3]) is None
    assert run_eval.spearman([1], [1]) is None
