"""IRP / e-way bill errors in plain words. The IRP's own code and message
are always shown and logged alongside - the plain line is a pointer to
what to fix, picked from words in the message (code meanings are never
guessed; the only code used is 2150, the IRP's documented duplicate-IRN
code, which the app handles by fetching the existing IRN)."""

import re

from app.einvoice.providers import ProviderError
from app.utils.gst_config import IRN_CANCEL_HOURS

DUPLICATE_IRN_CODE = "2150"

# (words that must all start a word in the lower-cased message, plain explanation) - first match wins.
_HINTS = [
    (("duplicate",), "The IRP already has this document - its IRN has been fetched where possible."),
    (("token",), "The login to the IRP / GSP has expired or was refused - check the API user name and "
                 "password under e-Invoice settings."),
    (("password",), "The API user name or password was refused - check them under e-Invoice settings."),
    (("whitelist",), "NIC hasn't whitelisted this server for direct API access - use a GSP, or ask NIC."),
    (("client.?id",), "The client ID / secret was refused - check them under e-Invoice settings."),
    (("client.?secret",), "The client ID / secret was refused - check them under e-Invoice settings."),
    (("buyer", "gstin"), "The IRP doesn't accept the customer's GSTIN - check it is right and active."),
    (("recipient", "gstin"), "The IRP doesn't accept the customer's GSTIN - check it is right and active."),
    (("seller", "gstin"), "The IRP doesn't accept your GSTIN for this login - check the GSTIN the API user "
                          "was created for."),
    (("pin",), "A PIN code doesn't match its state - check your PIN code (e-Invoice settings) and the "
               "customer's PIN code and state."),
    (("hsn",), "An HSN / SAC code isn't one the IRP knows - check the item's HSN code."),
    (("cancel",), f"The IRP won't cancel this - an IRN can only be cancelled once, within {IRN_CANCEL_HOURS} hours, and not "
                  "after an e-way bill is active on it. Issue a credit note instead."),
    (("date",), "The document date isn't accepted - it can't be in the future and, at your turnover, may be "
                "too old to report."),
    (("distance",), "The distance isn't accepted - the e-way bill system checks it against the two PIN codes."),
    (("vehicle",), "The vehicle number isn't in the accepted format (e.g. TN01AB1234)."),
    (("transporter",), "The transporter ID isn't valid - it is the transporter's GSTIN or TRANSIN."),
    (("value",), "The amounts don't add up the way the IRP checks them - open the document and check totals."),
    (("not authorised",), "The login was refused - check the details under e-Invoice settings."),
    (("connect",), "Couldn't reach the IRP / GSP - check the internet connection and try again. Nothing was "
                   "created."),
    (("timed out",), "The IRP / GSP didn't answer in time - try again in a minute. If it went through, the "
                     "app will pick up the existing IRN on the retry."),
]


def plain(error: ProviderError) -> str:
    if error.code == "CONFIG" or error.code == "KEY":
        return error.message
    if error.code and DUPLICATE_IRN_CODE in error.code.split(","):
        return _HINTS[0][1]
    text = (error.message or "").lower()
    for words, hint in _HINTS:
        if all(re.search(rf"\b{w}", text) for w in words):
            return hint
    return "The government system refused this - see its message below, fix the document, and try again."
