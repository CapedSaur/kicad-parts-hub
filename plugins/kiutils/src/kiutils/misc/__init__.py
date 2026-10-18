"""Miscellaneous package for kiutils

This package contains configuration constants and other miscellaneous utilities
that don't fit into other categories.

The configuration module contains default values used when creating new KiCad
files through the library's create_new() class methods.
"""

# Import configuration constants
from .config import KIUTILS_CREATE_NEW_GENERATOR_STR, KIUTILS_CREATE_NEW_VERSION_STR

# Export list for controlled imports
__all__ = [
    "KIUTILS_CREATE_NEW_GENERATOR_STR",  # Default generator string for new files
    "KIUTILS_CREATE_NEW_VERSION_STR",  # Default version string for new files
]
