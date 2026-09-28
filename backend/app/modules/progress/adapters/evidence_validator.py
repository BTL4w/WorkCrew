"""One bounded parser subprocess; no extraction or external-resource fetching."""

import resource
import sys
from pathlib import Path


def main() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    from app.modules.progress.adapters.filesystem_storage import detect_original_format
    from app.modules.progress.domain.evidence import EvidenceError

    try:
        print(detect_original_format(Path(sys.argv[1])))
    except EvidenceError:
        sys.exit(1)


if __name__ == "__main__":
    main()
