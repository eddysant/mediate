"""The one way mediate runs an external tool.

Every conversion is a subprocess call — ffmpeg, cwebp, avifenc, sips,
jpegtran — and all of them are invoked the same way: an argv list (never a
shell string, so a filename containing a quote or a space cannot become
syntax), output captured as text rather than inherited, and the command
logged at debug level so a failing run can be reproduced by hand from
conversion.log.

It lives in its own module because both the photo and the video pipelines
need it, and importing one from the other would be a cycle.
"""

from __future__ import annotations

import logging
import subprocess
from typing import List

log = logging.getLogger("mediate")


def run(cmd: List[str]) -> subprocess.CompletedProcess:
    log.debug("running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)
