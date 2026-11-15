"""Generate only this project's nginx config after operator supplies the domain."""
import argparse
import re
from pathlib import Path

def render(domain=None):
    root = Path(__file__).resolve().parents[2]
    if domain is None:
        return (root / 'deploy/nginx/conf.d/observer.conf').read_text()
    if not re.fullmatch(r'(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', domain):
        raise ValueError('invalid domain')
    if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in domain.split('.')):
        raise ValueError('invalid DNS label')
    return (root / 'deploy/nginx/https.conf.template').read_text().replace('__DOMAIN__', domain.lower())


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument('domain', nargs='?', help='Real DNS name, without https:// or path')
    parser.add_argument('--http', action='store_true', help='First bootstrap only; does not overwrite existing config')
    parser.add_argument('--output-dir', type=Path, default=root / '.runtime/nginx')
    args = parser.parse_args()
    if bool(args.domain) == args.http:
        parser.error('provide either a domain or --http')
    try:
        content = render(args.domain)
    except ValueError as exc:
        parser.error(str(exc))
    target = args.output_dir / 'observer.conf'
    if args.http and target.exists():
        parser.error('runtime config already exists; refusing to replace it with HTTP')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.conf.tmp')
    temporary.write_text(content)
    temporary.replace(target)
    print('Generated', target, '; validate nginx before reload. No tracked source was changed.')


if __name__ == '__main__':
    main()
