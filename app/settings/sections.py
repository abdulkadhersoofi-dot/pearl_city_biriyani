"""Settings in sections (redesign stage 2): the sections' menu, and the
module switches sorted as the one catalogue sorts them (console cleanup,
stage 1) with each module's "Set up" link.

A "Set up" link only shows once its module is on - its screen is a 404
until then, like every switched-off module."""

from flask import url_for

from app.utils import catalogue

# key, label, icon, endpoint, one-line description
SECTIONS = [
    ("business", "Business", "store", "settings.index",
     "Your type of business, turnover and how new items are priced."),
    ("appearance", "Appearance", "sparkles", "settings.appearance",
     "Logo, colours and font - on your screens, sign-in page and prints."),
    ("modules", "Modules", "layout-grid", "settings.features",
     "Switch on only what your business uses - everything else stays out of your menus."),
    ("counter", "Counter", "monitor", "settings.counter",
     "How bills are paid at the counter, and the counter's own setup."),
    ("documents", "Documents & printing", "printer", "settings.documents",
     "Print sizes and the wording printed on your documents."),
    ("security", "Sign-in & security", "shield-check", "settings.security",
     "Two-step sign-in, idle sign-out and the counter lock."),
    ("subscription", "Subscription", "banknote", "subscriptions.mine",
     "Your invoices for this software."),
]

# Set-up screens guarded by more than their own module's flag.
SETUP_ALSO_NEEDS = {"batch_expiry": ("inventory",), "serial_imei": ("inventory",)}


def section_menu(active: str) -> list[dict]:
    return [{"key": key, "label": label, "icon": icon, "url": url_for(endpoint), "active": key == active,
             "description": description}
            for key, label, icon, endpoint, description in SECTIONS]


def section_info(key: str) -> dict:
    for k, label, icon, _endpoint, description in SECTIONS:
        if k == key:
            return {"key": k, "label": label, "icon": icon, "description": description}
    raise KeyError(key)


def module_groups(tenant) -> list[dict]:
    """The Modules section: every built switch once, sorted as the one
    catalogue sorts it (app/utils/catalogue.py) - trade packs, modules in
    six areas, counter options - each with its "Set up" link once on."""
    out = []
    for group in catalogue.groups():
        items = []
        for row in group["items"]:
            feature, entry = row["feature"], row["entry"]
            key = feature.key
            on = tenant.has_feature(key)
            reachable = on and all(tenant.has_feature(k) for k in SETUP_ALSO_NEEDS.get(key, ()))
            items.append({"key": key, "label": feature.label, "description": feature.description, "icon": entry.icon,
                          "on": on, "depth": row["depth"],
                          "setup_url": url_for(entry.setup, **dict(entry.setup_kwargs)) if (entry.setup and reachable)
                          else None,
                          "setup_label": entry.setup_label,
                          "needs_off": catalogue.missing_needs(key, tenant.has_feature) if on else [],
                          **_package_bits(tenant, key)})
        out.append({"label": group["title"], "kind": group["kind"], "kind_title": group["kind_title"],
                    "kind_hint": group["kind_hint"], "first": group["first"], "items": items})
    return out


def _package_bits(tenant, key) -> dict:
    """Where the client's package puts a module (packages and powers,
    stage 3): Included (always on, locked), Client's choice (their switch)
    or Off - and, if Off but holding records, read-only with a link to
    them."""
    from app.utils.entitlements import READ_ONLY, module_mode, module_state
    from app.utils.module_data import home_endpoint

    state = module_state(tenant, key)
    read_only = module_mode(tenant, key) == READ_ONLY
    home = home_endpoint(key) if read_only else None
    return {"state": state, "read_only": read_only, "records_url": url_for(home) if home else None}
