"""Who the app talks to for IRNs and e-way bills - one interface, several
providers, picked per GSTIN under e-Invoice settings:

  nic_sandbox  NIC's own IRP sandbox (direct API access, test data only)
  nic          NIC IRP production (direct API access - needs NIC to have
               whitelisted the client's API user and public IP)
  gsp          any GST Suvidha Provider / ASP that passes the NIC API
               through as plain JSON - base URL, header names and paths
               are settings, so changing GSP is a settings change

A new provider is a subclass of Provider (app.einvoice.providers.base)
registered in PROVIDERS: only `fields` (what the settings form asks for)
and its eight methods need writing. Everything above this package -
logging every call, storing the session, plain-language errors - stays
the same.

Nothing here is ever faked: without a saved login, the app offers the
JSON download for the IRP / e-way bill portal's bulk-upload tools
instead, and the user records the IRN / EWB number they get back."""

from app.einvoice.providers.base import (  # noqa: F401
    CredentialField, EwbResult, IrnResult, Provider, ProviderError,
)
from app.einvoice.providers.gsp import GspProvider
from app.einvoice.providers.nic import NicProductionProvider, NicSandboxProvider

PROVIDERS = {p.key: p for p in (NicSandboxProvider, NicProductionProvider, GspProvider)}
