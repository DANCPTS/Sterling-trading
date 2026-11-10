"""Offline Grok decision layer for forward tests.

Providers return data. They do not place orders, write the operational ledger,
or change Desk Rules. The default provider is a seeded mock. The xAI class is
an unwired stub and does not open a socket.
"""

from sterling.research.grok.preregistration import PreRegistration
from sterling.research.grok.providers import MockGrokProvider, XaiGrokProvider
from sterling.research.grok.session import ForwardSession

__all__ = ["ForwardSession", "MockGrokProvider", "PreRegistration", "XaiGrokProvider"]
