"""Site accounts created by the agent (e.g. ATS portals that require a login).

Passwords are generated here, stored only in the agent state (encrypted with
JOBAGENT_STATE_KEY whenever it leaves your machine) and typed straight into the
browser. They are never shown to Claude and never printed in CI logs.
"""

from __future__ import annotations

import os
import secrets
import string
from urllib.parse import urlparse

from .store import Account, State

ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_=+"


def generate_password(length: int = 20) -> str:
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(length))
        # Most sites want at least one of each class.
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#$%^&*-_=+" for c in pw)):
            return pw


def site_of(url: str) -> str:
    host = (urlparse(url).netloc or url).lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def own_password() -> str:
    """The password you use for job-site accounts (secret JOBAGENT_SITE_PASSWORD), if you gave one:
    then the agent can sign in where you already have an account, and you can sign in to the
    accounts it creates."""
    return os.environ.get("JOBAGENT_SITE_PASSWORD", "")


def get_or_create(state: State, url: str, username: str) -> Account:
    site = site_of(url)
    acct = state.accounts.get(site)
    mine = own_password()
    if acct is None:
        acct = Account(site=site, username=username, password=mine or generate_password())
        state.accounts[site] = acct
        state.log(site, "account_created", f"username {username}")
    return acct  # an existing account keeps the password it was created with


def can_reveal() -> bool:
    """Passwords are only ever printed on your own machine, never in CI."""
    return os.environ.get("GITHUB_ACTIONS") != "true" and os.environ.get("CI") != "true"
