"""Utilities package for kiutils

This package contains utility functions for S-Expression parsing and string handling
that are used throughout the kiutils library.

Modules:
- sexpr: S-Expression parsing utilities for KiCad file formats
- strings: String manipulation utilities including dequote and prefix removal
"""

# Import the sexpr module (contains multiple functions)
from . import sexpr

# Import specific string utilities
from .strings import dequote, remove_prefix

# Export list for controlled imports
__all__ = [
    "sexpr",  # S-Expression parsing module
    "dequote",  # Remove quotes from strings
    "remove_prefix",  # Remove prefix from strings
]
