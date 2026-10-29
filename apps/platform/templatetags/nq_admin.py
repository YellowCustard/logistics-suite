"""Template helpers for the Nuronteq admin shell (admin/base_site.html, admin/index.html and admin/nq/*)."""

from django import template
from django.db.models import Case, IntegerField, Value, When
from django.urls import reverse
from django.utils.html import format_html, json_script
from django.utils.safestring import mark_safe

from apps.platform import admin_dashboard
from apps.platform.models import AuditEntry, ExceptionCase, ExceptionStatus, Severity

register = template.Library()

# Operational order, not alphabetical: the sections people open most come first.
APP_ORDER = ["oms", "wms", "tms", "masterdata", "platform"]
APP_ICONS = {"oms": "orders", "wms": "warehouse", "tms": "truck", "masterdata": "database", "platform": "cpu"}
TABS = [("oms", "Orders"), ("wms", "Warehouse"), ("tms", "Transport")]  # phone bottom bar, after Home
# Business-number prefix to the admin list that searches it (prefixes from the next_number() calls).
ID_ROUTES = {
    "ORD": "oms.salesorder",
    "ASN": "oms.inboundnotice",
    "SHP": "tms.shipment",
    "TRP": "tms.trip",
    "GRN": "wms.receipt",
    "WAV": "wms.pickwave",
    "CC": "wms.cyclecount",
}
SEVERITY_TONE = {Severity.CRITICAL: "critical", Severity.HIGH: "critical", Severity.MEDIUM: "warning"}
TONE_ICONS = {"critical": "alert", "warning": "clock"}

ICONS = {  # 24 px stroke icons; constant markup only
    "home": '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    "orders": '<rect x="8" y="2" width="8" height="4" rx="1"/>'
    '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M9 12h6M9 16h6"/>',
    "warehouse": '<path d="M2 21V8l10-5 10 5v13"/><path d="M6 21v-9h12v9M6 16h12"/>',
    "truck": '<path d="M1 4h14v12H1zM15 8h4l4 4v4h-8"/>'
    '<circle cx="5.5" cy="18.5" r="2"/><circle cx="18.5" cy="18.5" r="2"/>',
    "database": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/>'
    '<path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    "cpu": '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/>'
    '<path d="M9 2v3M15 2v3M9 19v3M15 19v3M19 9h3M19 15h3M2 9h3M2 15h3"/>',
    "box": '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "eye": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "pencil": '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4z"/><path d="m14.5 5.5 3 3"/>',
    "external": '<path d="M14 4h6v6M10 14 20 4M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
    "panel": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    "alert": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'
    '<path d="M12 9v4M12 17h.01"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "activity": '<path d="M22 12h-4l-3 8L9 4l-3 8H2"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 9.2-9.2M17 6l3 3M14 9l2 2"/>',
    "logout": '<path d="M9 21H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h4M16 17l5-5-5-5M21 12H9"/>',
}


def _ordered(apps):
    rank = {label: i for i, label in enumerate(APP_ORDER)}
    return sorted(apps, key=lambda a: (rank.get(a["app_label"], len(rank)), str(a["name"])))


def _current(path: str, url: str) -> str:
    """aria-current value: "page" on the page itself, "true" anywhere below it."""
    return "page" if path == url else ("true" if path.startswith(url) else "")


@register.simple_tag
def nq_icon(name: str, css: str = "size-5"):
    return format_html(
        '<svg class="{}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>',
        css,
        mark_safe(ICONS[name]),  # noqa: S308 - ICONS holds constant markup only
    )


@register.simple_tag
def nq_app_icon(app_label: str, css: str = "size-5"):
    return nq_icon(APP_ICONS.get(app_label, "box"), css)


@register.simple_tag
def nq_tone_icon(tone: str):
    return nq_icon(TONE_ICONS[tone], "size-3.5") if tone in TONE_ICONS else ""


@register.simple_tag
def nq_nav(request, available_apps):
    """Sidebar, drawer and bottom-bar entries with their current-page state."""
    path, index = request.path, reverse("admin:index")
    apps = []
    for app in _ordered(available_apps):
        models = [{**m, "current": _current(path, m["admin_url"])} for m in app["models"] if m.get("admin_url")]
        icon = APP_ICONS.get(app["app_label"], "box")
        apps.append({**app, "models": models, "icon": icon, "open": path.startswith(app["app_url"])})
    by_label = {a["app_label"]: a for a in apps}
    tabs = [{"url": index, "label": "Home", "icon": "home", "current": "page" if path == index else ""}]
    tabs += [
        {
            "url": by_label[label]["app_url"],
            "label": name,
            "icon": APP_ICONS[label],
            "current": _current(path, by_label[label]["app_url"]),
        }
        for label, name in TABS
        if label in by_label
    ]
    return {"apps": apps, "tabs": tabs, "home": path == index}


@register.simple_tag
def nq_palette_data(available_apps):
    """JSON for the command palette: only the lists and add forms this user may open."""
    items = []
    for app in _ordered(available_apps):
        for m in app["models"]:
            key = f"{app['app_label']}.{m['object_name'].lower()}"
            if m.get("admin_url"):
                hint = "View" if m.get("view_only") else "Edit"
                items.append(
                    {
                        "key": key,
                        "group": str(app["name"]),
                        "label": str(m["name"]),
                        "url": m["admin_url"],
                        "hint": hint,
                    }
                )
            if m.get("add_url"):
                items.append(
                    {
                        "key": f"{key}:add",
                        "group": "Create",
                        "label": f"Add {m['model']._meta.verbose_name}",
                        "url": m["add_url"],
                        "hint": "Add",
                    }
                )
    return json_script({"items": items, "ids": ID_ROUTES}, "nq-palette-data")


@register.simple_tag(takes_context=True)
def nq_kpis(context):
    return admin_dashboard.kpis(context["request"])


@register.simple_tag(takes_context=True)
def nq_chips(context, app_label):
    return admin_dashboard.chips(context["request"], app_label)


@register.simple_tag(takes_context=True)
def nq_attention(context, limit=5):
    """The most urgent open exceptions: severity first, then the earliest due date."""
    if not context["request"].user.has_perm("platform.view_exceptioncase"):
        return None
    rank = Case(
        When(severity=Severity.CRITICAL, then=Value(0)),
        When(severity=Severity.HIGH, then=Value(1)),
        When(severity=Severity.MEDIUM, then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    )
    cases = (
        ExceptionCase.objects.exclude(status=ExceptionStatus.RESOLVED)
        .annotate(rank=rank)
        .order_by("rank", "due_at", "-created_at")[:limit]
    )
    return {
        "all_url": admin_dashboard.changelist(
            "platform.exceptioncase", status__in=f"{ExceptionStatus.OPEN},{ExceptionStatus.IN_PROGRESS}"
        ),
        "rows": [
            {
                "case": c,
                "tone": SEVERITY_TONE.get(c.severity, "neutral"),
                "url": reverse("admin:platform_exceptioncase_change", args=[c.pk]),
            }
            for c in cases
        ],
    }


@register.simple_tag(takes_context=True)
def nq_recent(context, limit=8):
    if not context["request"].user.has_perm("platform.view_auditentry"):
        return None
    return list(AuditEntry.objects.order_by("-occurred_at")[:limit])


@register.filter
def nq_ordered(app_list):
    return _ordered(app_list)


@register.filter
def nq_verbose_name(model):
    return model["model"]._meta.verbose_name


@register.filter
def nq_initials(user):
    words = (getattr(user, "full_name", "") or "").split()
    if words:
        return "".join(w[0] for w in words[:2]).upper()
    return (user.get_username()[:1] or "?").upper()
