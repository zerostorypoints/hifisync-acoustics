import re

import hifisync_acoustics


def test_version_is_semver():
    assert re.match(r"^\d+\.\d+\.\d+$", hifisync_acoustics.__version__)
