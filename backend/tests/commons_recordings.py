from deckly.domain.notes.basic import BasicFields
from deckly.domain.notes.note import Note
from tests.domain.builders import SOURCES, client_id
from tests.fakes import commons_page, commons_results

LETTERBOXES = "File:Letterboxes Ocotillo Wells 2013.jpg"
LETTERBOXES_CROP = "File:Letterboxes Ocotillo Wells 2013 Crop.jpg"
MALAYSIAN_STOP_SIGN = "File:Malaysia road sign RP1.svg"
LAOTIAN_STOP_SIGN = "File:Luang-Prabang Laos Stop-Sign-01.jpg"
PERSON_AT_STOP_SIGN = (
    "File:A person in blue standing next to a stop sign in Anseremme, Belgium (DSCF7416).jpg"
)
STOP_SIGNS = frozenset({MALAYSIAN_STOP_SIGN, LAOTIAN_STOP_SIGN})
LETTERBOX_ALT = "Letterboxes at Ocotillo Wells, CA, USA."
MALAYSIAN_STOP_SIGN_ALT = "Stop."
TUXYSO = '<a href="//commons.wikimedia.org/wiki/User:Tuxyso" title="User:Tuxyso">Tuxyso</a>'
TUXYSO_CREDIT = (
    'Tuxyso / <a href="//commons.wikimedia.org/wiki/Main_Page" title="Main Page">Wikimedia Commons</a>'
)
CEPHOTO = (
    '<a href="//commons.wikimedia.org/wiki/User:Cccefalon" title="User:Cccefalon">CEphoto, Uwe Aranas</a>'
)
CEPHOTO_CREDIT = "Photo by CEphoto, Uwe Aranas <b>or alternatively</b> © CEphoto, Uwe Aranas"
JPEG: dict[str, object] = {"mime": "image/jpeg"}

STOP_SIGN_SEARCH = commons_results(
    commons_page(
        1,
        LETTERBOXES,
        info=JPEG,
        categories=(
            "5841 (house number)",
            "5855 (house number)",
            "5861 (house number)",
            "Letter boxes in California",
            "Objects in San Diego County, California",
            "Photographs of red octagonal stop signs",
        ),
        license_code="cc-by-sa-3.0",
        attribution_required="true",
        description=LETTERBOX_ALT,
        artist=TUXYSO,
        credit_line=TUXYSO_CREDIT,
    ),
    commons_page(
        2,
        LETTERBOXES_CROP,
        info=JPEG,
        categories=(
            "5841 (house number)",
            "5855 (house number)",
            "5861 (house number)",
            "Colorado Desert",
            "Letter boxes in California",
            "Objects in San Diego County, California",
        ),
        license_code="cc-by-sa-3.0",
        attribution_required="true",
        description="Letterboxes at Ocotillo Wells, CA, USA",
        artist=TUXYSO,
        credit_line=TUXYSO_CREDIT,
    ),
    commons_page(
        3,
        MALAYSIAN_STOP_SIGN,
        categories=(
            "Diagrams of Malay-language road signs",
            "Diagrams of octagonal stop signs",
            "SVG priority road signs - stop",
            "Stop signs in Malaysia",
        ),
        license_code="pd",
        attribution_required="false",
        description=MALAYSIAN_STOP_SIGN_ALT,
        artist="Public Works Department Malaysia",
    ),
    commons_page(
        4,
        LAOTIAN_STOP_SIGN,
        info=JPEG,
        categories=("2010 in Laos", "Lao-language road signs", "Stop signs in Laos"),
        license_code="cc-by-sa-3.0",
        attribution_required="true",
        description='Luang Prabang, Laos:  Regulatory sign "Stop"',
        artist=CEPHOTO,
        credit_line=CEPHOTO_CREDIT,
    ),
    commons_page(
        5,
        PERSON_AT_STOP_SIGN,
        info=JPEG,
        license_code="cc-by-4.0",
        attribution_required="true",
        restrictions="personality",
        description="A person in blue standing next to a stop sign in Anseremme, Belgium",
        artist='<a href="//commons.wikimedia.org/wiki/User:Trougnouf">Trougnouf (Benoit Brummer)</a>',
    ),
)

STOP_SIGN_NOTE = Note(
    client_id=client_id(1),
    fields=BasicFields(front="What does a red octagonal road sign mean?", back="Stop and give way"),
    sources=SOURCES,
)
