"""Test settings: 4 CPU threads, the Agg backend, figures closed after each test."""

import matplotlib
import pytest
import torch

matplotlib.use("Agg")
torch.set_num_threads(4)


@pytest.fixture(autouse=True)
def _close_figures():
    yield
    import matplotlib.pyplot as plt

    plt.close("all")
