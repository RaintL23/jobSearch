from backend.scraping import _extract_hiring_permalink, is_linkedin_hiring_permalink


class _Handle:
    def __init__(self, element=None):
        self._element = element

    def as_element(self):
        return self._element


class _Element:
    def __init__(self, *, attrs=None, selectors=None, closest=None):
        self.attrs = attrs or {}
        self.selectors = selectors or {}
        self.closest = closest or {}

    def get_attribute(self, name):
        return self.attrs.get(name)

    def query_selector(self, selector):
        value = self.selectors.get(selector)
        if isinstance(value, list):
            return value[0] if value else None
        return value

    def query_selector_all(self, selector):
        value = self.selectors.get(selector, [])
        return value if isinstance(value, list) else [value]

    def evaluate_handle(self, _expression, selector):
        return _Handle(self.closest.get(selector))

    def evaluate(self, _expression):
        return ""


def test_hiring_permalink_prefers_posts_url_and_removes_tracking():
    anchor = _Element(
        attrs={
            "href": (
                "https://www.linkedin.com/posts/example_share-7484508470059188224-0YRO/"
                "?utm_source=social_share_send"
            )
        }
    )
    card = _Element(selectors={"a[href*='/posts/']": anchor})

    assert _extract_hiring_permalink(card) == (
        "https://www.linkedin.com/posts/example_share-7484508470059188224-0YRO/"
    )


def test_hiring_permalink_finds_activity_in_parent_container():
    parent = _Element(attrs={"data-urn": "urn:li:activity:7484508470059188224"})
    card = _Element(closest={"div.feed-shared-update-v2": parent})

    assert _extract_hiring_permalink(card) == (
        "https://www.linkedin.com/feed/update/urn:li:activity:7484508470059188224/"
    )


def test_hiring_permalink_supports_ugc_post_identifier():
    parent = _Element(attrs={"data-urn": "urn:li:ugcPost:7484508470059188224"})
    card = _Element(closest={"div[data-urn*='ugcPost']": parent})

    assert _extract_hiring_permalink(card) == (
        "https://www.linkedin.com/feed/update/urn:li:ugcPost:7484508470059188224/"
    )


def test_hiring_permalink_never_returns_search_url():
    card = _Element()

    assert _extract_hiring_permalink(card) == ""


def test_permalink_validator_rejects_non_specific_linkedin_pages():
    invalid = (
        "https://www.linkedin.com/showcase/sussex-jobs-brighton/",
        "https://www.linkedin.com/showcase/crawley-jobs-sussex/",
        "https://www.linkedin.com/company/hisabdo-expense-management-app/posts/",
        "https://www.linkedin.com/search/results/content/?keywords=%23Hiring",
        "https://www.linkedin.com/in/some-recruiter/",
    )
    assert all(not is_linkedin_hiring_permalink(url) for url in invalid)


def test_permalink_validator_accepts_only_individual_posts():
    valid = (
        "https://www.linkedin.com/posts/marcelina-kalinowska-a457153aa_"
        "position-details-vx-18-back-end-net-share-7484508470059188224-0YRO/",
        "https://www.linkedin.com/feed/update/urn:li:activity:7484508470059188224/",
        "https://www.linkedin.com/feed/update/urn:li:ugcPost:7484508470059188224/",
    )
    assert all(is_linkedin_hiring_permalink(url) for url in valid)


def test_extractor_rejects_company_posts_page():
    anchor = _Element(
        attrs={
            "href": (
                "https://www.linkedin.com/company/"
                "hisabdo-expense-management-app/posts/"
            )
        }
    )
    card = _Element(selectors={"a[href*='/posts/']": anchor})

    assert _extract_hiring_permalink(card) == ""


def test_hiring_intent_rejects_open_to_work():
    from backend.scraping import _linkedin_hiring_intent

    assert _linkedin_hiring_intent(
        "We're #hiring a Backend Developer Node.js (Portugal, remote). View job"
    )
    assert not _linkedin_hiring_intent(
        "I'm currently open to new opportunities as a Senior PHP Backend Developer. "
        "Nikita is open to work. View job preferences"
    )
    # Caso real: título con pipes; el slug del permalink trae _hiring- por el hashtag.
    otw_title = "Open to Work | .NET Developer | Immediate Joiner Hello Connect"
    hiring_slug = (
        "https://www.linkedin.com/posts/example_"
        "hiring-dotnet-share-7484508470059188224-0YRO/"
    )
    assert not _linkedin_hiring_intent(otw_title, permalink=hiring_slug)


def test_hiring_intent_accepts_truncated_latam_recruiter_post():
    """
    Caso real: María Fernanda Spirito — en el feed solo se ve el snippet
    «NUEVA OPORTUNIDAD | .NET API DEVELOPER …more»; el #Hiring está después.
    El permalink canónico sí incluye _hiring- en el slug.
    """
    from backend.scraping import _linkedin_hiring_intent

    snippet = (
        "🚀 NUEVA OPORTUNIDAD INTERNACIONAL | .NET API DEVELOPER 🚀 "
        "¿Sos especialista en .NET Core, AWS y arquitecturas Serverless? "
        "¿Te gustaría participar …more"
    )
    permalink = (
        "https://www.linkedin.com/posts/spiritomariafernanda_"
        "hiring-dotnetdeveloper-backenddeveloper-share-7485421228564905985-atO7/"
    )
    assert _linkedin_hiring_intent(snippet, permalink=permalink)
    # Sin permalink y solo snippet corto: "nueva oportunidad" ya alcanza.
    assert _linkedin_hiring_intent(snippet)


def test_hiring_location_rejects_india_and_requires_latam():
    from backend.analysis.local import linkedin_hiring_location_ok

    locs = ["Argentina", "LATAM", "Remoto LATAM"]
    assert (
        linkedin_hiring_location_ok(
            "WE'RE HIRING | Multiple IT Openings Across India",
            "ar",
            locs,
        )
        is False
    )
    assert (
        linkedin_hiring_location_ok(
            "Bangalore | Immediate Joiner | .NET Full Stack Developer",
            "ar",
            locs,
        )
        is False
    )
    assert (
        linkedin_hiring_location_ok(
            "We're hiring a .NET Developer. Apply now!",
            "ar",
            locs,
        )
        is False
    )
    assert (
        linkedin_hiring_location_ok(
            "We're hiring a .NET Developer Remoto LATAM. Join our team!",
            "ar",
            locs,
        )
        is True
    )
    assert (
        linkedin_hiring_location_ok(
            "NUEVA OPORTUNIDAD INTERNACIONAL | .NET API DEVELOPER",
            "ar",
            locs,
        )
        is True
    )


def test_activity_id_published_fallback():
    from backend.scraping.sources.linkedin_hiring import (
        _linkedin_activity_published_at,
    )

    iso = _linkedin_activity_published_at(
        "https://www.linkedin.com/feed/update/urn:li:activity:7484508470059188224/"
    )
    assert iso is not None
    assert iso.startswith("2026-07-")
