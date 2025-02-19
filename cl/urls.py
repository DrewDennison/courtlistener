from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.sitemaps import views as sitemaps_views
from django.urls import include, path, re_path, register_converter
from django.views.decorators.cache import cache_page
from django.views.generic import RedirectView

from cl.audio.sitemap import AudioSitemap
from cl.lib.converters import BlankSlugConverter
from cl.opinion_page.sitemap import DocketSitemap, OpinionSitemap
from cl.people_db.sitemap import PersonSitemap
from cl.search.models import SEARCH_TYPES
from cl.simple_pages.sitemap import SimpleSitemap
from cl.sitemap import cached_sitemap
from cl.visualizations.sitemap import VizSitemap

register_converter(BlankSlugConverter, "blank-slug")

sitemaps = {
    SEARCH_TYPES.ORAL_ARGUMENT: AudioSitemap,
    SEARCH_TYPES.OPINION: OpinionSitemap,
    SEARCH_TYPES.RECAP: DocketSitemap,
    SEARCH_TYPES.PEOPLE: PersonSitemap,
    "visualizations": VizSitemap,
    "simple": SimpleSitemap,
}

urlpatterns = [
    # Admin docs and site
    path("admin/", admin.site.urls),
    path("", include("cl.audio.urls")),
    path("", include("cl.opinion_page.urls")),
    path("", include("cl.simple_pages.urls")),
    path("", include("cl.users.urls")),
    path("", include("cl.favorites.urls")),
    path("", include("cl.people_db.urls")),
    path("", include("cl.search.urls")),
    path("", include("cl.alerts.urls")),
    path("", include("cl.api.urls")),
    path("", include("cl.donate.urls")),
    path("", include("cl.visualizations.urls")),
    path("", include("cl.stats.urls")),
    # Sitemaps
    path(
        "sitemap.xml",
        cache_page(60 * 60 * 24 * 14, cache="db_cache")(sitemaps_views.index),
        {"sitemaps": sitemaps, "sitemap_url_name": "sitemaps"},
    ),
    path(
        "sitemap-<str:section>.xml",
        cached_sitemap,
        {"sitemaps": sitemaps},
        name="sitemaps",
    ),
    # Redirects
    path(
        "privacy/",
        RedirectView.as_view(url="/terms/#privacy", permanent=True),
    ),
    path(
        "removal/",
        RedirectView.as_view(url="/terms/#removal", permanent=True),
    ),
] + static("/", document_root=settings.MEDIA_ROOT)
