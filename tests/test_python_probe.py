import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from refiner.checks import check_one, validate_plan

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT/'skill-refiner/scripts/refiner/python_probe.py'


class PythonObservation(unittest.TestCase):
    def test_multiline_commands_are_separated_but_quoted_newlines_are_not_commands(self):
        check={'type':'tool_command','value':r'^python3\s+first\.py$'}
        def trace(command):
            return [{'role':'tool_call','tool_call':{'id':'1','name':'shell','arguments':{'command':command}}}]
        self.assertTrue(check_one(check,'',ROOT,trace("/bin/zsh -lc 'python3 first.py\npython3 second.py'"))[0])
        self.assertFalse(check_one(check,'',ROOT,trace("printf '%s' '\npython3 first.py\n'"))[0])
    def test_forwarded_json_stream_matches_real_tool_outputs_without_newline_requirement(self):
        check={'type':'tool_output_matches','value':r'python3 probe.py','control_command':'python3 probe.py'}
        trace=[]
        for number,output in enumerate(['{"stock":{"a":2,"b":1}}','{"status":"invalid"}']):
            trace += [{'role':'tool_call','tool_call':{'id':str(number),'name':'shell','arguments':{'command':'python3 probe.py'}}},
                      {'role':'tool_result','tool_result':{'call_id':str(number),'status':'success','content':output}}]
        self.assertTrue(check_one(check,'{"stock":{"b":1,"a":2}}{"status":"invalid"}',ROOT,trace)[0])
        self.assertFalse(check_one(check,'{"stock":{"a":9,"b":1}}{"status":"invalid"}',ROOT,trace)[0])
        self.assertFalse(check_one(check,'{"status":"invalid"}{"stock":{"a":2,"b":1}}',ROOT,trace)[0])
    def test_json_sequence_ignores_serialization_but_checks_values_order_and_count(self):
        check={'type':'json_sequence_equal','value':[{'a':1,'b':2},{'status':'invalid'}]}
        self.assertTrue(check_one(check,' {"b":2,"a":1}\n {\n"status":"invalid"\n}',ROOT)[0])
        for response in ['{"a":9,"b":2}\n{"status":"invalid"}', '{"status":"invalid"}\n{"a":1,"b":2}',
                         '{"a":1,"b":2}', '[{"a":1,"b":2},{"status":"invalid"}]',
                         'Explanation: {"a":1,"b":2}\n{"status":"invalid"}']:
            self.assertFalse(check_one(check,response,ROOT)[0],response)

    def test_executor_rejects_observer_tampering_outside_installed_skill(self):
        import refiner.execute as adapter
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'skill').mkdir()
            (root/'skill/SKILL.md').write_text('original Skill')
            (root/'refiner-python-probe.py').write_text('original observer')
            sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
            (root/'profile.json').write_text(json.dumps({'backend':'codex','_skill_manifest':{'SKILL.md':sha(root/'skill/SKILL.md')},
                '_helper_manifest':{'refiner-python-probe.py':sha(root/'refiner-python-probe.py')}}))
            (root/'request.json').write_text(json.dumps({'messages':[]}))
            def execute(*args):
                (root/'refiner-python-probe.py').write_text('tampered observer')
                return {'exit_code':0,'final_message':'claimed success','transcript':[],
                        'metadata':{'backend':'codex'},'artifacts':{'files':[]}}
            with patch.object(adapter.Path,'cwd',return_value=root),patch.object(adapter,'execute',side_effect=execute), \
                 patch.object(adapter.sys,'argv',['execute.py',str(root/'profile.json'),str(root/'request.json'),str(root/'result.json')]):
                adapter.main()
            result=json.loads((root/'result.json').read_text())
            self.assertEqual(result['exit_code'],1)
            self.assertEqual(result['final_message'],'')
            self.assertIn('refiner-python-probe.py',result['stderr'])

    def observe(self, script, extra_files=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'input.json').write_text('{"value":2}\r\n',newline='')
            before = (root/'input.json').read_bytes()
            (root/'business.py').write_text(script)
            for name, content in (extra_files or {}).items():
                (root/name).write_text(content)
            done = subprocess.run([sys.executable,str(PROBE),'--evidence','observed.json','--track','input.json',
                                   '--protect-inputs','--deny-network','--deny-subprocess','business.py','input.json'],
                                  cwd=root,text=True,capture_output=True,timeout=20)
            self.assertEqual(done.returncode,0,done.stderr)
            evidence=json.loads((root/'observed.json').read_text())
            self.assertEqual((root/'input.json').read_bytes(),before)
            return done,evidence

    def test_actual_stdout_exit_code_and_side_effect_attempts_are_separate(self):
        done,evidence=self.observe('import json,sys\nprint(json.dumps({"value":2}))\nsys.exit(7)\n')
        self.assertEqual(json.loads(done.stdout),{'value':2})
        self.assertEqual(evidence['exit_code'],7)
        self.assertEqual(evidence['stdout_json'],{'value':2})
        self.assertTrue(evidence['inputs_unchanged'])
        for code,field in [('open("input.json","w").write("changed")','write_events'),
                           ('import socket\nsocket.socket()','network_events'),
                           ('import subprocess\nsubprocess.run(["true"])','subprocess_events')]:
            with self.subTest(field=field):
                _,observed=self.observe(code)
                self.assertTrue(observed[field])
                self.assertEqual(observed['exit_code'],1)

    def test_observed_script_can_import_its_sibling_module(self):
        done,evidence=self.observe('from business_helper import VALUE\nprint(VALUE)\n', {'business_helper.py':'VALUE = 37\n'})
        self.assertEqual(evidence['exit_code'],0)
        self.assertEqual(done.stdout.strip(),'37')

    def test_new_file_checks_reject_wrong_values_and_whitespace_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'evidence.json').write_text('{"result":{"exit_code":0}}')
            check={'type':'file_json_path_equal','path':'evidence.json','pointer':'/result/exit_code','value':0}
            self.assertTrue(check_one(check,'',root)[0])
            check['value']=1
            self.assertFalse(check_one(check,'',root)[0])
            (root/'input.txt').write_bytes(b'value\r\n')
            self.assertTrue(check_one({'type':'file_text_equal','path':'input.txt','value':'value\r\n'},'',root)[0])
            self.assertFalse(check_one({'type':'file_text_equal','path':'input.txt','value':'value\n'},'',root)[0])


if __name__=='__main__':
    unittest.main()
