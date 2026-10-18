"""Fixed self-audit infrastructure; business fixtures and tests remain model-planned."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

from workflow_verifier import digest, manifest, verify_nested

PYTHON = '/Volumes/work/skill-guide/.venv/bin/python'
SKILL_UP = '/Volumes/work/skill-guide/.tools/skill-up'


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def safe(root, name):
    rel = Path(name)
    assert not rel.is_absolute() and rel.parts and '..' not in rel.parts
    result = (root/rel).resolve()
    assert result.is_relative_to(root.resolve())
    return result


def unchanged(root, before):
    # Installed roots may equal the task workspace. New task outputs are allowed;
    # each pre-existing file must still exist with exactly its original bytes.
    assert all((root/name).is_file() and hashlib.sha256((root/name).read_bytes()).hexdigest() == sha
               for name, sha in before.items()), 'A pre-existing installed/input file changed'


def invoke(argv, cwd, timeout, record):
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    save(record, {'argv':argv, 'exit_code':proc.returncode, 'stdout':proc.stdout, 'stderr':proc.stderr})
    return proc


def refine(installed, fixture):
    base = Path.cwd().resolve()
    before = manifest(installed)
    supplied = load(fixture)
    assert {'skill_files','requirements','standard','script','probes'} <= supplied.keys()
    assert 'SKILL.md' in supplied['skill_files'] and len(supplied['probes']) >= 3
    child = base/'child'
    child.mkdir()
    for name, content in supplied['skill_files'].items():
        assert isinstance(content, str)
        p = safe(child,name); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(content)
    child_before = manifest(child)
    (base/'business.txt').write_text(supplied['requirements'])
    (base/'standard.txt').write_text(supplied['standard'])
    config = {'schema_version':1, 'agents':{'default':{'backend':'codex'}},
              'executors':[{'id':'codex','backend':'codex'}]}
    save(base/'config.json',config)

    def probes(target, phase):
        observations = []
        for index, probe in enumerate(supplied['probes']):
            data = base/(phase+'-input-'+str(index)+'.json'); save(data,probe['input'])
            proc = invoke([PYTHON,str(safe(target,supplied['script'])),str(data)],base,30,
                          base/(phase+'-probe-'+str(index)+'.json'))
            try: output=json.loads(proc.stdout)
            except ValueError: output=None
            observations.append(proc.returncode == 0 and output == probe['expected'])
        return observations

    assert not all(probes(child,'original')), 'Fixture lacks a demonstrated independent contract defect'
    run = base/'workflow'
    argv = [PYTHON,str(installed/'scripts/refine.py'),'run',str(child),
            '--spec',str(base/'business.txt'),'--standard',str(base/'standard.txt'),
            '--config',str(base/'config.json'),'--skill-up',SKILL_UP,'--output',str(run),
            '--max-rounds','2','--max-cases','2','--acceptance-cases','2',
            '--max-agent-calls','16','--max-evaluations','12','--max-seconds','900']
    with tempfile.TemporaryDirectory(prefix='trusted-refiner-cwd-') as cwd:
        proc = invoke(argv,cwd,960,base/'workflow-process.json')
    assert proc.returncode == 0, proc.stdout+'\n'+proc.stderr
    state = load(run/'state.json')
    assert state['status'] == 'passed'
    unchanged(installed,before)
    assert manifest(child) == child_before
    assert state['agent_calls'] <= 16 and state['target_evaluations'] <= 12
    evidence = load(run/'final/evidence.json')
    assert evidence['original_manifest'] == child_before
    final = Path(state['final_skill'])
    assert evidence['candidate_manifest'] == manifest(final)
    assert all(probes(final,'candidate')), 'Repaired implementation fails independent supplied contract probes'
    patch = (run/'final/changes.patch').read_text()
    assert supplied['script'] in patch, 'Contract defect must be repaired in supporting implementation'
    import re,yaml
    def identity(path):
        text=(path/'SKILL.md').read_text();m=re.match(r'---\s*\n(.*?)\n---',text,re.S)
        return yaml.safe_load(m[1])['name']
    assert identity(child) == identity(final)
    inputs = load(run/'inputs.json')
    assert inputs['requirements'] == [{'source':str(base/'business.txt'),'content':supplied['requirements']}]
    assert any(isinstance(s,dict) and s.get('content') == supplied['standard'] for s in inputs['standards'])
    live_roles = []
    for path in sorted((run/'agent-calls').glob('*/execution.json')):
        execution=load(path); backend=load(path.with_name('backend-result.json'))
        assert execution['exit_code'] == backend['exit_code'] == 0
        assert execution['backend'] in {'codex','codex_subagent'}
        raw=next(a['content'] for a in backend['artifacts']['files'] if a['name']=='adapter-events.jsonl')
        events=[json.loads(line) for line in raw.splitlines() if line.startswith('{')]
        assert any(e.get('type')=='turn.completed' for e in events)
        live_roles.append(execution['role'])
    assert {'plan','acceptance-plan','plan-review','repair','standards-review'} <= set(live_roles)
    archive=base/'trusted-workflow.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for path in sorted(run.rglob('*')):
            if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts:
                z.write(path,'workflow/'+path.relative_to(run).as_posix())
    with zipfile.ZipFile(archive) as z:
        detail=verify_nested(z,'workflow/',state)
    assert load(run/'oracle.lock.json')['checker_sha256'] == hashlib.sha256((installed/'scripts/refiner/checks.py').read_bytes()).hexdigest()
    save(base/'trusted-audit-evidence.json',{'verified':True,'workflow':str(run),'argv':argv,
         'installed_original_files':before,'child_original_files':child_before,
         'independent_verification':detail,'live_roles':live_roles,
         'trusted_auditor_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    return {'workflow_verified':True,'source_preserved':True,'oracle_locked':True,
            'real_model_evidence':True,'fresh_acceptance_verified':True,'candidate_probes_verified':True,
            'optional_backends':{'cursor':'unverified','custom_api':'unverified'}}


def diagnostics(installed, fixture):
    base=Path.cwd().resolve();before=manifest(installed)
    tag=load(fixture)['tag'];assert isinstance(tag,str) and re_full_tag(tag)
    missing=str(base/('nonexistent-'+tag));assert not Path(missing).exists()
    configs={
        'missing':{'schema_version':1,'agents':{'default':{'backend':'command','command':[missing]}},
                   'executors':[{'id':'missing','backend':'command','command':[missing,'{input_file}','{output_file}']}]},
        'shape':{'schema_version':1,'agents':[],'executors':[{'id':'codex','backend':'codex'}]},
        'json':'{ invalid '+tag+' json',
    }
    records=[]
    with tempfile.TemporaryDirectory(prefix='trusted-doctor-cwd-') as cwd:
        for label,config in configs.items():
            path=base/(label+'-config.json')
            path.write_text(config if isinstance(config,str) else json.dumps(config))
            original=path.read_bytes()
            argv=[PYTHON,str(installed/'scripts/refine.py'),'doctor','--config',str(path)]
            proc=invoke(argv,cwd,60,base/(label+'-process.json'))
            assert proc.returncode != 0 and 'traceback' not in (proc.stdout+proc.stderr).lower()
            assert proc.stdout.strip() or proc.stderr.strip()
            if label=='missing':
                result=json.loads(proc.stdout);assert result['ready'] is False
                assert result['profiles'] and all(p['ready'] is False for p in result['profiles'])
            assert path.read_bytes()==original
            records.append(argv)
    unchanged(installed,before)
    save(base/'trusted-audit-evidence.json',{'verified':True,'argv':records,
         'trusted_auditor_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    return {'missing_executable_rejected':True,'malformed_configuration_rejected':True,
            'inputs_preserved':True,'no_traceback':True}


def re_full_tag(tag):
    import re
    return re.fullmatch(r'[a-z0-9-]{1,40}',tag) is not None


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['refine','diagnostics']);parser.add_argument('installed_root');parser.add_argument('fixture')
    args=parser.parse_args()
    installed=Path(args.installed_root).resolve();assert (installed/'SKILL.md').is_file()
    result=(refine if args.mode=='refine' else diagnostics)(installed,Path(args.fixture).resolve())
    print(json.dumps(result))
