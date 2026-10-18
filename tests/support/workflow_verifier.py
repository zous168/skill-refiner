"""Independent, read-only post-run evidence gate for the self-refinement."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
IGNORED = {'.git', '.venv', 'node_modules', '__pycache__', '.tools', '.refiner-runs'}

def load(path):
    return json.loads(Path(path).read_text())

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def manifest(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()
            and not any(part in IGNORED for part in p.relative_to(root).parts)}

def verify_nested(z, prefix, state):
    def read(name):
        return json.loads(z.read(prefix+name))
    def zip_manifest(directory):
        start=prefix+directory.rstrip('/')+'/'
        return {n[len(start):]:hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()
                if n.startswith(start) and not n.endswith('/')
                and not any(p in IGNORED for p in Path(n[len(start):]).parts)}
    plan, acceptance, inputs = [read(name) for name in ('test-plan.json','acceptance-plan.json','inputs.json')]
    lock, evidence = read('oracle.lock.json'), read('final/evidence.json')
    assert lock['sha256'] == digest({'plan':plan,'acceptance':acceptance,'inputs':inputs})
    assert plan['requirements'] == acceptance['requirements']
    assert not plan['coverage_gaps'] and not acceptance['coverage_gaps']
    assert any(digest(c['files'] or {'prompt':c['prompt']}) not in
               {digest(d['files'] or {'prompt':d['prompt']}) for d in plan['cases']} for c in acceptance['cases'])
    child=Path(state['source']).name
    assert lock['source_manifest'] == evidence['original_manifest'] == zip_manifest('original/'+child)
    assert evidence['candidate_manifest'] == zip_manifest('final/'+child)
    assert evidence['oracle_hash'] == lock['sha256']
    assert z.read(prefix+'final/changes.patch').strip()
    assert any(c['status']=='FAIL' for c in evidence['baseline']['cases'])
    assert all(c['status']!='ERROR' for c in evidence['baseline']['cases'])
    for phase, cases in [('regression',plan['cases']),('acceptance',acceptance['cases'])]:
        result=evidence[phase]
        assert result['passed'] and all(c['status']=='PASS' and c['passed'] for c in result['cases'])
        assert result['suite_sha256'] == digest(cases)
        for engine in result['engines']:
            relative=Path(engine['upstream_report']).relative_to(state['workspace']).as_posix()
            report=read(relative)
            assert report['case_results'] and all(r['status']=='PASS' for r in report['case_results'])
    assert all(not r['regressed_cases'] for r in state['rounds'] if r['accepted'])
    assert not any(f['severity']=='error' for f in evidence['standards'])
    return {'original_manifest':evidence['original_manifest'], 'candidate_manifest':evidence['candidate_manifest'],
            'oracle_sha256':lock['sha256'],'development_cases':[c['id'] for c in plan['cases']],
            'acceptance_cases':[c['id'] for c in acceptance['cases']]}
