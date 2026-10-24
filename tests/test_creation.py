import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from refiner.common import RefinerError, digest, manifest
from refiner.creation import DRAFT_INSTRUCTION, normalize_draft, validate_analysis, validate_analysis_review
from refiner.pipeline import make_plan
from refiner.profiles import role_profile
from refiner.standards import AUTHORING

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT/'tests/fixtures/creation_agent.py'


def analyze():
    result = subprocess.run([sys.executable, str(FIXTURE)], input=json.dumps({'role': 'analyze', 'input': {}}),
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


class CreationContracts(unittest.TestCase):
    def test_creation_and_repair_guidance_uses_conditional_evidence_first_questions(self):
        for guidance in (DRAFT_INSTRUCTION, AUTHORING):
            with self.subTest(guidance=guidance[:40]):
                self.assertIn('inspect', guidance)
                self.assertIn('host', guidance)
                self.assertIn('secrets', guidance)
                self.assertIn('conditional', guidance)

    def test_named_packaging_directory_is_unwrapped_without_changing_contents(self):
        draft={'files':[{'path':'example/SKILL.md','content':'original instructions'},
                        {'path':'example/scripts/run.py','content':'print(37)'}]}
        normalized,mapping=normalize_draft(draft,'example')
        self.assertEqual([f['path'] for f in normalized['files']],['SKILL.md','scripts/run.py'])
        self.assertEqual([f['content'] for f in normalized['files']],[f['content'] for f in draft['files']])
        self.assertEqual(draft['files'][0]['path'],'example/SKILL.md')
        self.assertEqual(normalize_draft(draft,'another')[0],draft)
        self.assertEqual(mapping['example/SKILL.md'],'SKILL.md')
    def test_analysis_requires_all_categories_and_grounded_requirement_coverage(self):
        analysis = analyze()
        validate_analysis(analysis)
        validate_analysis(analysis, source_ids={'business_description'})
        wrong_source = copy.deepcopy(analysis)
        wrong_source['requirements'][0]['source'] = 'input.standards: authoring rule'
        with self.assertRaisesRegex(RefinerError, 'original business source'):
            validate_analysis(wrong_source, source_ids={'business_description'})
        for broken in [dict(analysis, scenarios=[analysis['scenarios'][0]]*3),
                       dict(analysis, name='different-name')]:
            with self.assertRaises(RefinerError):
                validate_analysis(broken, 'decimal-total')
        broken = copy.deepcopy(analysis)
        broken['scenarios'][0]['requirement_ids'] = ['invented']
        with self.assertRaisesRegex(RefinerError, 'unknown'):
            validate_analysis(broken)
        review = {'approved': True, 'issues': [], 'requirement_assessments': [
            {'requirement_id': r['id'], 'grounded': True, 'basis': 'Explicit request'} for r in analysis['requirements']]}
        validate_analysis_review(review, analysis)
        review['requirement_assessments'][0]['grounded'] = False
        with self.assertRaisesRegex(RefinerError, 'grounding'):
            validate_analysis_review(review, analysis)

    def test_creation_role_can_differ_from_repair_and_defaults_to_repair(self):
        profiles = {'agents': {'default': {'backend': 'command'},
                               'repair': {'backend': 'codex_subagent'}, 'creation': {'backend': 'cursor'}}}
        self.assertEqual(role_profile(profiles, 'draft')['backend'], 'cursor')
        del profiles['agents']['creation']
        self.assertEqual(role_profile(profiles, 'draft')['backend'], 'codex_subagent')
        self.assertEqual(role_profile(profiles, 'analyze')['backend'], 'command')
        del profiles['agents']['default']
        with self.assertRaisesRegex(RefinerError, 'No backend configured'):
            role_profile(profiles, 'analyze')

    def test_test_planner_cannot_drop_error_cases_or_change_creation_contract(self):
        analysis = analyze()
        class Agent:
            def __init__(self, mutation):
                self.mutation = mutation
            def ask(self, role, instruction, payload, schema):
                out = subprocess.run([sys.executable, str(FIXTURE)], input=json.dumps({'role': role, 'input': payload}),
                                     text=True, capture_output=True, check=True)
                plan = json.loads(out.stdout)
                if role == 'plan':
                    if self.mutation == 'kind':
                        plan['cases'][-1]['kind'] = 'normal'
                    else:
                        plan['requirements'][0]['behavior'] = 'A substituted business rule'
                return plan
        with tempfile.TemporaryDirectory() as tmp:
            for mutation, message in [('kind','Missing required scenario'), ('contract','frozen capability contract')]:
                with self.subTest(mutation=mutation), self.assertRaisesRegex(RefinerError, message):
                    make_plan(Agent(mutation), {'contract': analysis['requirements'], 'required_case_kinds': ['normal','boundary','error']}, Path(tmp), 4)

    def test_plan_revision_preserves_previous_plan_and_can_use_third_attempt(self):
        analysis=analyze()
        class Agent:
            calls=0
            def ask(self,role,instruction,payload,schema):
                if role=='plan':
                    self.calls+=1
                    if self.calls>1:
                        self_test.assertIn('previous_plan',payload)
                        self_test.assertTrue(payload['validation_feedback'])
                out=subprocess.run([sys.executable,str(FIXTURE)],input=json.dumps({'role':role,'input':payload}),
                                   text=True,capture_output=True,check=True)
                result=json.loads(out.stdout)
                if role=='plan' and self.calls<3:
                    result['cases'][-1]['kind']='normal'
                return result
        self_test=self
        agent=Agent()
        with tempfile.TemporaryDirectory() as tmp:
            plan=make_plan(agent,{'contract':analysis['requirements'],'required_case_kinds':['normal','boundary','error']},Path(tmp),4,max_attempts=3)
        self.assertEqual(agent.calls,3)
        self.assertEqual({c['kind'] for c in plan['cases']},{'normal','boundary','error'})


class CreationIntegration(unittest.TestCase):
    def invoke(self, root, *, env_extra=None, calls=24):
        engine = {'name': 'creation-script', 'custom': {'transport': 'local', 'response_format': 'session_result',
                  'local': {'command': sys.executable, 'args': [str(ROOT/'tests/fixtures/script_engine.py'), '${input_file}', '${output_file}'],
                            'input_file': '${input_file}', 'output_file': '${output_file}'}}}
        config = {'schema_version': 1, 'agents': {'default': {'backend': 'command', 'command': [sys.executable,str(FIXTURE)]}},
                  'executors': [{'id': name, 'backend': 'skillup', 'engine': engine} for name in ['engine-a','engine-b']]}
        (root/'config.json').write_text(json.dumps(config))
        command = [sys.executable, '-m', 'refiner.cli', 'create',
                   'Sum signed decimal amounts exactly to two places; empty array is zero; non-array amounts return invalid-input error. Use bundled script.',
                   '--name', 'decimal-total', '--config', str(root/'config.json'), '--output', str(root/'run'),
                   '--destination', str(root/'delivered'), '--timeout', '30', '--max-agent-calls', str(calls)]
        env = os.environ.copy()
        env.update(env_extra or {})
        done = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, timeout=180)
        state = json.loads((root/'run/state.json').read_text())
        return done, state

    @unittest.skipUnless((ROOT/'.tools/skill-up').exists(), 'skill-up required')
    def test_create_two_round_repair_matrix_and_acceptance_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            done, state = self.invoke(root, env_extra={'REFINER_CREATE_WRAP':'1'})
            self.assertEqual(done.returncode, 0, done.stdout+done.stderr+json.dumps(state))
            self.assertEqual(state['status'], 'created')
            self.assertEqual(state['verification_status'], 'verified')
            self.assertEqual(json.loads((root/'run/verification.json').read_text())['status'], 'verified')
            stage_manifest = json.loads((root/'run/stages.json').read_text())
            self.assertEqual(stage_manifest['schema_version'], 'skill-refiner.stages/v1')
            self.assertEqual(stage_manifest['status'], 'created')
            self.assertTrue(all(stage['status'] == 'completed' for stage in stage_manifest['stages']))
            self.assertTrue(any(item['path'] == 'analysis.json' and item.get('sha256')
                                for item in stage_manifest['stages'][1]['artifacts']))
            child = json.loads((root/'run/refinement/state.json').read_text())
            self.assertEqual(len(child['rounds']), 2)
            child_stages = json.loads((root/'run/refinement/stages.json').read_text())
            self.assertEqual(child_stages['schema_version'], 'skill-refiner.stages/v1')
            self.assertEqual(child_stages['status'], 'passed')
            self.assertTrue(all(stage['status'] == 'completed' for stage in child_stages['stages']))
            self.assertEqual(json.loads((root/'run/refinement/verification.json').read_text())['status'], 'verified')
            self.assertEqual(state['agent_calls'], state['creation_agent_calls'] + child['agent_calls'])
            self.assertLessEqual(state['agent_calls'], 24)
            self.assertEqual(manifest(root/'delivered'), manifest(Path(state['final_skill'])))
            self.assertNotEqual(manifest(root/'run/drafts/1/decimal-total'), manifest(root/'delivered'))
            for filename in ['test-plan.json','acceptance-plan.json']:
                plan = json.loads((root/'run/refinement'/filename).read_text())
                self.assertEqual({c['kind'] for c in plan['cases']}, {'normal','boundary','error'})
            evidence = json.loads((root/'run/refinement/final/evidence.json').read_text())
            self.assertFalse(evidence['baseline']['passed'])
            for phase in ['regression','acceptance']:
                self.assertTrue(evidence[phase]['passed'])
                self.assertEqual(len({e['suite_sha256'] for e in evidence[phase]['engines']}), 1)
            self.assertTrue((root/'run/final/creation.patch').read_text())
            self.assertTrue((root/'run/final/changes.patch').read_text())
            resumed=subprocess.run([sys.executable,'-m','refiner.cli','create','--resume-from',str(root/'run'),
                '--config',str(root/'config.json'),'--output',str(root/'resumed'),'--timeout','30'],
                cwd=ROOT,text=True,capture_output=True,timeout=180)
            self.assertEqual(resumed.returncode,0,resumed.stdout+resumed.stderr)
            resume_state=json.loads((root/'resumed/state.json').read_text())
            self.assertEqual(resume_state['status'],'created')
            self.assertEqual(resume_state['creation_agent_calls'],0)
            self.assertFalse(resume_state['creation_provenance']['reuses_verdicts'])
            resume_child=json.loads((root/'resumed/refinement/state.json').read_text())
            self.assertEqual(resume_child['plan_provenance'],str((root/'run/refinement').resolve()))
            self.assertEqual(len(resume_child['rounds']),2)
            self.assertFalse(json.loads((root/'resumed/refinement/final/evidence.json').read_text())['baseline']['passed'])

    @unittest.skipUnless((ROOT/'.tools/skill-up').exists(), 'skill-up required')
    def test_failed_acceptance_withholds_both_final_and_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            done, state = self.invoke(root, env_extra={'REFINER_TEST_ACCEPT_FAIL':'1'})
            self.assertEqual(done.returncode, 2, done.stdout+done.stderr)
            self.assertEqual(state['status'], 'failed_acceptance')
            self.assertFalse((root/'delivered').exists())
            self.assertFalse((root/'run/final').exists())

    def test_material_questions_stop_before_draft_and_shared_budget_cannot_reset(self):
        for extra, calls, expected in [({'REFINER_CREATE_CLARIFY':'1'},24,'needs_clarification'), ({},3,'error')]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                done,state=self.invoke(root,env_extra=extra,calls=calls)
                self.assertEqual(done.returncode,2,done.stdout+done.stderr)
                self.assertEqual(state['status'],expected)
                self.assertFalse((root/'delivered').exists())
                self.assertFalse((root/'run/refinement').exists())
                if expected=='needs_clarification':
                    self.assertFalse((root/'run/drafts').exists())
                    self.assertTrue(state['questions'])
                else:
                    self.assertEqual(state['agent_calls'],3)


if __name__ == '__main__':
    unittest.main()
