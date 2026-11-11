"""Imported first by every repo tool that imports nagasim or product (review S8-15): the tool sees
the shipped registries only, never a plugin folder this machine's user trusted.

Importing the engine's agent tools, or running a model, loads the user's trusted plugin folders
(`nagasim.plugins.load_plugins`), so a tool that lists the registries or writes a committed file
from them would count a client's block types on the laptop that ran it. Pointing NAGASIM_USER_DIR
at an empty folder and NAGASIM_PLUGIN_PATH at nothing is what tests/conftest.py does too. A
caller that already set NAGASIM_USER_DIR (the tests, a person on purpose) keeps it.
"""
import os
import tempfile

if "NAGASIM_USER_DIR" not in os.environ:
    os.environ["NAGASIM_USER_DIR"] = tempfile.mkdtemp(prefix="naga-tool-user-")
    os.environ["NAGASIM_PLUGIN_PATH"] = ""
