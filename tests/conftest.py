"""
Shared pytest fixtures for the radoff test suite.

Standard `pytest-homeassistant-custom-component` boilerplate: it ships its
own pytest plugin (fixtures for `hass`, event loop handling, etc.) and one
fixture, `enable_custom_integrations`, that has to be pulled in explicitly -
otherwise Home Assistant's component loader ignores everything under
`custom_components/` and `async_setup_entry(DOMAIN, ...)` fails with
"Integration 'radoff' not found" for every test.
"""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):  # noqa: ANN001, ARG001
    """Make custom_components/radoff loadable by hass in every test."""
    return
