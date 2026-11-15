"""Persist only a successfully deployed commit tag; never edit operator .env."""
import argparse
import re
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('commit')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        parser.error('expected a full Git commit hash')
    target = Path(__file__).resolve().parents[2] / '.runtime/active-image-tag'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(args.commit + '\n')
    temporary.replace(target)


if __name__ == '__main__':
    main()
