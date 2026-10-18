"""Deterministic creation-controller double; target scripts still run in real skill-up."""
import json
import os
from pathlib import Path
import subprocess
import sys

request = json.load(sys.stdin)
role, payload = request['role'], request['input']

def original(role, payload):
    output = subprocess.run([sys.executable, str(Path(__file__).with_name('protocol_agent.py'))],
                            input=json.dumps({'role': role, 'input': payload}), text=True, capture_output=True, check=True)
    return json.loads(output.stdout)

requirements = original('plan', {})['requirements'] + [
    {'id': 'R-invalid', 'behavior': 'Non-array amounts return an invalid-input error', 'source': 'business_description', 'required': True}]
for requirement in requirements:
    requirement['source'] = 'business_description: explicit monetary behavior'

if role == 'analyze':
    response = {'name': 'decimal-total', 'goal': 'Compute exact monetary totals with controlled errors',
        'requirements': requirements,
        'workflow': [{'id': 'W1', 'action': 'Validate amounts then sum exactly', 'inputs': 'JSON ledger file',
                      'outputs': 'total or error JSON', 'failure_behavior': 'Return invalid-input error',
                      'requirement_ids': [r['id'] for r in requirements]}],
        'scenarios': [{'kind': kind, 'applicable': True, 'situation': situation,
                       'expected_behavior': behavior, 'basis': 'Explicit business description', 'requirement_ids': [rid]}
                      for kind, situation, behavior, rid in [('normal', 'Signed cents', 'Exact total', 'R-total'),
                          ('boundary', 'Empty amounts', 'Zero total', 'R-empty'),
                          ('error', 'Non-array amounts', 'Invalid-input error', 'R-invalid')]],
        'assumptions': [], 'external_dependencies': [],
        'blocking_questions': ['Which monetary rounding rule is required?'] if os.environ.get('REFINER_CREATE_CLARIFY') else []}
elif role == 'analysis-review':
    response = {'approved': True, 'issues': [], 'requirement_assessments': [
        {'requirement_id': r['id'], 'grounded': True, 'basis': 'The business request explicitly specifies this behavior.'}
        for r in payload['analysis']['requirements']]}
elif role == 'draft':
    script = '''import json
import sys
from pathlib import Path
values = json.loads(Path(sys.argv[1]).read_text())["amounts"]
if not isinstance(values, list):
    print(json.dumps({"error": "invalid-input"}))
    sys.exit(0)
if not values:
    print(json.dumps({"error": "empty-not-supported"}))
    sys.exit(0)
total = sum(int(float(value)) for value in values)
print(json.dumps({"total": f"{total:.2f}"}))
'''
    response = {'implementation_notes': 'Intentionally defective first draft for genuine two-round pipeline verification.',
        'files': [{'path': path, 'content': content, 'reason': 'Implement an independently specified business behavior',
                   'requirement_ids': [r['id'] for r in requirements]} for path, content in [
            ('SKILL.md', '---\nname: decimal-total\ndescription: Sum signed decimal amounts from local JSON ledgers.\n---\n'
             'Run the installed `scripts/total.py INPUT_FILE` with Python and forward its JSON output unchanged. '
             'Amounts must be an array; return invalid-input error otherwise. Preserve cents; empty sum is zero.\n'),
            ('scripts/total.py', script)]]}
elif role in {'plan', 'acceptance-plan'}:
    response = original(role, payload)
    response['requirements'] = payload.get('contract', requirements)
    suffix = 'accept' if role == 'acceptance-plan' else 'dev'
    response['cases'].append({'id': suffix+'-invalid', 'requirement_ids': ['R-invalid'], 'kind': 'error',
        'prompt': 'Use the installed decimal-total script on input.json; return only its JSON unchanged.',
        'files': {'input.json': '{"amounts":null}' if suffix == 'dev' else '{"amounts":{}}'},
        'checks': [{'type': 'json_equal', 'value': {'error': 'invalid-input'}},
                   {'type': 'tool_output_matches', 'value': r'python.*scripts/total\.py',
                    'control_command': 'python3 scripts/total.py input.json'}],
        'oracle_reason': 'The independent business requirement rejects non-array amounts with a controlled error.',
        'good_response': '{"error":"invalid-input"}', 'bad_response': '{"total":"0.00"}'})
    for case in response['cases']:
        case['helpers'] = ['python_probe']
        case['checks'] += [{'type':'file_json_path_equal','path':'observed.json','pointer':'/exit_code','value':0},
                           {'type':'file_json_path_equal','path':'observed.json','pointer':'/inputs_unchanged','value':True}]
else:
    response = original(role, payload)
if role == 'draft' and os.environ.get('REFINER_CREATE_WRAP'):
    for file in response['files']:
        file['path'] = 'decimal-total/' + file['path']
print(json.dumps(response))
