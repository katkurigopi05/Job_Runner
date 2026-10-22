"""Score frozen private answer reviews; never calls a model or database.

python -m scripts.score_answer_support storage/evaluation/answers.json [--provisional]
Input is a JSON array of AnswerReview records. Output contains aggregates only.
"""

import argparse
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from apps.api.answer_support import AnswerReview, score
from packages.core.config import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--provisional", action="store_true")
    args = parser.parse_args()
    try:
        path = args.input.resolve()
        if not path.is_relative_to(Path(get_settings().storage_root).resolve()):
            parser.error("private answer reviews must stay under storage_root")
        records = TypeAdapter(list[AnswerReview]).validate_json(path.read_bytes())
        report = score(records, provisional=args.provisional)
    except (OSError, ValidationError, ValueError):
        # Pydantic's normal message includes input values, possibly private text.
        parser.error("invalid or unfinished review; validate privately or use --provisional")
    print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
