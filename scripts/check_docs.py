#!/usr/bin/env python3
"""Check local Markdown links in maintained guides; historical research is excluded."""
from pathlib import Path
import re
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
GUIDES = [ROOT / p for p in ('README.md', 'CONTRIBUTING.md', 'THIRD_PARTY_NOTICES.md', 'research/README.md')]
LINK = re.compile(r'!?\[[^\]]*\]\(([^)]+)\)')


def main():
    errors = []
    documents = GUIDES + sorted((ROOT / 'docs').glob('*.md'))
    for document in documents:
        for target in LINK.findall(document.read_text()):
            target = target.split()[0].strip('<>')
            if re.match(r'[a-zA-Z][\w+.-]*:', target) or target.startswith('#'):
                continue
            path = unquote(target.split('#', 1)[0])
            if path and not (document.parent / path).exists():
                errors.append(f'{document.relative_to(ROOT)}: missing {target}')
    print('\n'.join(errors) if errors else f'Local links OK ({len(documents)} maintained guides)')
    return bool(errors)


if __name__ == '__main__':
    sys.exit(main())
