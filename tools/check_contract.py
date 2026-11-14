"""Проверка спецификации и примеров; не тест работающего HTTP-сервера."""
from copy import deepcopy
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = yaml.safe_load((ROOT / 'docs/api/openapi.yaml').read_text(encoding='utf-8'))


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def resolve(pointer):
    if not pointer.startswith('#/'):
        raise ValueError(f'Unexpected external reference: {pointer}')
    value = SPEC
    for part in pointer[2:].split('/'):
        value = value[part.replace('~1', '/').replace('~0', '~')]
    return value


def validator(schema):
    # Локальные ссылки компонентов остаются разрешимыми и при проверке фрагмента.
    return Draft202012Validator(
        {**schema, 'components': SPEC['components']}, format_checker=FormatChecker()
    )


def schema_validator(name):
    return validator({'$ref': f'#/components/schemas/{name}'})


def main():
    validate(SPEC)
    refs = [node['$ref'] for node in walk(SPEC) if '$ref' in node]
    for pointer in refs:
        resolve(pointer)
    examples = 0
    for node in walk(SPEC):
        if 'schema' not in node:
            continue
        values = [example['value'] for example in node.get('examples', {}).values()]
        if 'example' in node:
            values.append(node['example'])
        for value in values:
            validator(node['schema']).validate(value)
            examples += 1

    # Примеры обоих Tool также проверяются по соответствующим схемам данных API.
    tool_text = (ROOT / 'docs/tool-contract.md').read_text(encoding='utf-8')
    tool_examples = re.findall(r'```json\n(.*?)\n```', tool_text, re.S)
    for block in tool_examples:
        result = json.loads(block)
        assert set(result) == {'status', 'operation', 'data'} and result['status'] == 'ok'
        name = {'find_duplicates': 'DuplicateData', 'assess_quality': 'QualityData'}[result['operation']]
        schema_validator(name).validate(result['data'])

    valid_request = {'dataset_id': 'demo-01', 'operation': 'find_duplicates'}
    invalid_requests = [
        {}, {'dataset_id': 'demo-01'}, {'operation': 'find_duplicates'},
        {**valid_request, 'operation': 'remove_photos'},
        {**valid_request, 'operation': 7}, {**valid_request, 'dataset_id': ''},
        {**valid_request, 'dataset_id': 'x' * 65}, {**valid_request, 'dataset_id': 'bad id'},
        {**valid_request, 'url': 'https://example.com/personal.jpg'},
        {**valid_request, 'threshold': 50}, [], None,
    ]
    negative_count = 0

    def must_reject(name, value):
        nonlocal negative_count
        assert not schema_validator(name).is_valid(value), f'{name} accepted invalid value: {value}'
        negative_count += 1

    for value in invalid_requests:
        must_reject('CreateJob', value)
    key_schema = SPEC['paths']['/jobs']['post']['parameters'][0]['schema']
    for key in ['', 'a' * 129, 'has space', 'кириллица']:
        assert not validator(key_schema).is_valid(key), f'Accepted invalid key: {key}'
        negative_count += 1
    done = deepcopy(SPEC['paths']['/jobs/{id}']['get']['responses']['200']['content']['application/json']['examples']['done']['value'])
    cancel_response = SPEC['paths']['/jobs/{id}/cancel']['post']['responses']['200']
    cancelled = cancel_response['content']['application/json']['examples']['cancelled']['value']
    must_reject('Job', {**cancelled, 'started_at': done['started_at']})
    must_reject('Job', {**cancelled, 'finished_at': None})
    must_reject('Job', {**done, 'finished_at': None})
    must_reject('Job', {**done, 'state': 'failed', 'error': None})
    must_reject('Job', {**done, 'id': 'not-a-uuid'})
    must_reject('Job', {**done, 'created_at': 'yesterday'})
    must_reject('Job', {**done, 'image_ids': ['A', 'A']})
    failure = {'code': 'internal_error', 'message': 'Ошибка', 'details': []}
    must_reject('Job', {**done, 'error': failure})
    must_reject('Job', {**cancelled, 'error': failure})
    report = deepcopy(SPEC['paths']['/jobs/{id}/result']['get']['responses']['200']['content']['application/json']['examples']['duplicates']['value'])
    must_reject('Report', {**report, 'operation': 'assess_quality'})
    bad_group = {'sha256': 'abc', 'image_ids': ['A', 'B']}
    must_reject('DuplicateGroup', bad_group)
    must_reject('DuplicateGroup', {'sha256': 'a' * 64, 'image_ids': ['A']})
    must_reject('JobFailure', {**failure, 'code': 'undocumented_failure'})
    print('OpenAPI 3.1: OK')
    print(f'Local references resolved: {len(refs)}')
    print(f'Valid HTTP/header examples: {examples}; Tool examples: {len(tool_examples)}')
    print(f'Invalid values rejected: {negative_count}')
    print('No HTTP server, database or live Picsum was tested.')


if __name__ == '__main__':
    main()
