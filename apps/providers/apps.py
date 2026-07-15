from django.apps import AppConfig


class ProvidersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.providers"
    label = "providers"

    def ready(self) -> None:
        # Import provider modules so their @register decorators run and populate the registry.
        from . import simplefin, teller  # noqa: F401
        from .prices import stooq, yahoo, yahoo_chart  # noqa: F401
        from .scrapers import css  # noqa: F401
