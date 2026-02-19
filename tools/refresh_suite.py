#!/usr/bin/env python3
import argparse
import csv
import json
import random
import urllib.request
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with path.open('r', encoding='utf-8') as f:
        return json.load(f)


def fetch_text(url: str, timeout_seconds: int = 30) -> str:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        return response.read().decode('utf-8')


def render_template(template: str, row: dict[str, Any]) -> str:
    result = template
    for key, value in row.items():
        result = result.replace(f'{{{{{key}}}}}', str(value if value is not None else ''))
    return result


def prompts_from_source(source: dict[str, Any], timeout_seconds: int) -> list[str]:
    source_format = str(source['format']).lower()
    raw_text = fetch_text(str(source['url']), timeout_seconds=timeout_seconds)

    prompts: list[str] = []
    if source_format == 'json':
        rows = json.loads(raw_text)
        if not isinstance(rows, list):
            raise ValueError('source JSON must be an array')
        field = source.get('field')
        template = source.get('template')
        if not field and not template:
            raise ValueError("source requires either 'field' or 'template'")
        for row in rows:
            if not isinstance(row, dict):
                continue
            payload = render_template(str(template), row) if template else str(row.get(str(field), ''))
            payload = payload.strip()
            if payload:
                prompts.append(payload)
    elif source_format == 'csv':
        field = source.get('field')
        template = source.get('template')
        if not field and not template:
            raise ValueError("source requires either 'field' or 'template'")
        for row in csv.DictReader(raw_text.splitlines()):
            payload = render_template(str(template), row) if template else str(row.get(str(field), ''))
            payload = payload.strip()
            if payload:
                prompts.append(payload)
    else:
        raise ValueError(f"unsupported source format: {source_format}")

    if bool(source.get('shuffle', False)):
        random.Random(42).shuffle(prompts)
    limit = int(source.get('limit', 0))
    if limit > 0:
        prompts = prompts[:limit]

    return prompts


def main() -> None:
    parser = argparse.ArgumentParser(description='Refresh suite_external from prompt sources.')
    parser.add_argument(
        '--manifest',
        default='cases/prompt_sources_promptfoo.json',
        help='Path to external prompt manifest JSON',
    )
    parser.add_argument(
        '--external-out',
        default='cases/suite_external.json',
        help='Path to output external suite JSON',
    )
    parser.add_argument('--carrier', default='label', help='Carrier for generated external cases (label/env/cmd)')
    parser.add_argument('--id-prefix', default='cyberseceval_en', help='Prefix for generated external case IDs')
    parser.add_argument('--source-id', default='promptfoo_cyberseceval_en', help='Source id from manifest to use')
    parser.add_argument('--timeout-seconds', type=int, default=30, help='HTTP timeout for external source fetch')
    args = parser.parse_args()

    manifest = load_json(Path(args.manifest))
    sources = manifest.get('sources', []) if isinstance(manifest, dict) else []
    if not isinstance(sources, list) or not sources:
        raise ValueError('manifest must contain non-empty sources array')

    selected = None
    for source in sources:
        if str(source.get('id')) == args.source_id:
            selected = source
            break
    if selected is None:
        raise ValueError(f"source id not found in manifest: {args.source_id}")

    prompts = prompts_from_source(selected, timeout_seconds=args.timeout_seconds)

    generated_cases = [
        {
            'id': f"{args.id_prefix}_{idx:04d}",
            'carrier': args.carrier,
            'payload': prompt,
        }
        for idx, prompt in enumerate(prompts, start=1)
    ]

    external_out_path = Path(args.external_out)
    external_out_path.write_text(
        json.dumps(generated_cases, indent=2, ensure_ascii=True) + '\n',
        encoding='utf-8',
    )

    print(f'refreshed_external: {external_out_path}')
    print(f'generated_cases: {len(generated_cases)}')


if __name__ == '__main__':
    main()
