"""
This package initializes the necessary modules and classes for the project.
"""

from .base_model import BasicClassifier
from .resnet import ResNet

__all__ = ['BasicClassifier', 'ResNet']
