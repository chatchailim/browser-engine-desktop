import copy
import ctypes
import hashlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import rfc8785
from perception.desktop_tree import cleanup_owned_process_tree, DesktopTreeParser, FILETIME
from tests import rehearsal_runner as rr
from tests.rehearsal_validation import validate_results

def evidence():
    rid='test_closeout'; task='rehearsal_e_'+rid
    a={'window_found':True,'tree_parsed':True,'synthetic_content_found':True,
       'tree_nonempty':True,'element_count':2,'structure_hash':'sha256:'+'b'*64,
       'window_title_hash':'sha256:'+'c'*64,'cleanup_verified':True,
       'cleanup_audit':{'verified':True},'config_restored_cleanly':True,
       'config_copy_pre_hash':'a'*64,'config_copy_post_hash':'a'*64,
       'ownership_proof':{'verified':True},'cursor_valid':True,
       'cursor_initial':[1,2],'cursor_final':[1,2],'cursor_displacement':0,
       'input_methods_tested':10,'input_methods_blocked':True,'fact_desktop_spooled':True,
       'secret_in_tree':False,'secret_in_trace':False,'secret_in_fact':False,'secret_in_log':False}
    names=['click','double_click','right_click','mouse_move','drag_and_drop','type_text','press_key','hotkey','scroll','wait_until']
    a['method_details']={n:{'result':False,'blocked':True,'cursor_before':[1,2],'cursor_after':[1,2],'step_displacement':0} for n in names}
    d={'factId':'desktop-test','factType':'FactDesktopWindowRead','subjectId':'desktop-task:'+task,
       'hashAlgorithm':'jcs-sha256-v1','payload':{'taskId':task,'processName':'notepad.exe','elementCount':2,'structureHash':a['structure_hash'],'windowTitleHash':a['window_title_hash']}}
    h='sha256:'+hashlib.sha256(rfc8785.dumps(d)).hexdigest(); d['contentHash']=h
    return {'run_id':rid,'mode':'targeted_round_e','targeted_round':'round_e',
            'overall_ok':True,'overall_status':'TARGETED_ROUND_E_COMPLETED',
            'round_e':{'task_id':task,'status':'completed','audit':a},
            'facts':[{'factId':d['factId'],'factType':d['factType'],'hash_valid':True,'contentHash':h,'fact_json':d}],
            'manifest':{'run_id':rid,'mode':'targeted_round_e','rounds':{'round_e':{'task_id':task}},
                        'facts':{d['factId']:{'fact_type':d['factType'],'content_hash':h,'valid':True}}}}

class CloseoutTests(unittest.TestCase):
    def test_document_body_pattern_hashed_instead_of_editor_label(self):
        with patch.object(DesktopTreeParser, '_init_windows_apis'):
            parser = DesktopTreeParser()
        parser.policy = MagicMock()
        parser.policy.is_password_element.return_value = False
        parser._uia_module = MagicMock()
        elem = MagicMock()
        elem.CurrentControlType = 50030
        elem.CurrentName = 'Editor label'
        elem.CurrentAutomationId = 'document'
        elem.CurrentClassName = 'editor'
        elem.CurrentIsPassword = 0
        pattern = elem.GetCurrentPattern.return_value.QueryInterface.return_value
        pattern.DocumentRange.GetText.return_value = 'synthetic body'
        node = parser._extract_masked_element_node(elem)
        self.assertEqual(node['text_hash'], 'sha256:' + hashlib.sha256(b'synthetic body').hexdigest())
        self.assertNotIn('synthetic body', json.dumps(node))
        parser.policy.is_password_element.return_value = True
        elem.GetCurrentPattern.reset_mock()
        node = parser._extract_masked_element_node(elem)
        self.assertIsNone(node['text_hash'])
        elem.GetCurrentPattern.assert_not_called()

    def api(self, *, created=123, image='notepad.exe', wait=None):
        api=MagicMock(); api.GetProcessId.return_value=50
        def times(h,c,*rest):
            ctypes.cast(c,ctypes.POINTER(FILETIME)).contents.dwLowDateTime=created
            return 1
        api.GetProcessTimes.side_effect=times
        def name(h,f,buf,size): buf.value=image; return bool(image)
        api.QueryFullProcessImageNameW.side_effect=name
        api.WaitForSingleObject.side_effect=wait or [258,0]
        api.TerminateProcess.return_value=1
        return api

    def cleanup(self,api,**kw):
        with patch('os.kill',side_effect=AssertionError('PID kill forbidden')):
            return cleanup_owned_process_tree(50,expected_creation_time=123,retained_handle=777,kernel32=api,**kw)

    def test_retained_handle_killed_and_waited_without_reopen(self):
        api=self.api(); a=self.cleanup(api)
        self.assertTrue(a['verified']); api.TerminateProcess.assert_called_once_with(777,1)
        api.OpenProcess.assert_not_called(); api.CloseHandle.assert_not_called()

    def test_reused_unknown_image_unknown_time_wait_failure_never_kill(self):
        cases=[self.api(created=999),self.api(image=''),self.api(wait=[0xFFFFFFFF])]
        bad=self.api();bad.GetProcessTimes.side_effect=None;bad.GetProcessTimes.return_value=0;cases.append(bad)
        for api in cases:
            with self.subTest():
                self.assertFalse(self.cleanup(api)['verified']);api.TerminateProcess.assert_not_called()

    def test_unproven_child_never_killed(self):
        api=self.api();self.assertFalse(self.cleanup(api,child_pids={51})['verified']);api.TerminateProcess.assert_not_called()

    def test_confirmed_exited_handle_requires_no_termination(self):
        api=self.api(wait=[0]);self.assertTrue(self.cleanup(api)['verified']);api.TerminateProcess.assert_not_called()

    def test_exit_timeout_does_not_report_success(self):
        self.assertFalse(self.cleanup(self.api(wait=[258,258]))['verified'])

    def test_valid_targeted_evidence_passes(self):
        self.assertEqual(validate_results(evidence(),'test_closeout',True),(True,'Targeted evidence verified'))

    def test_negative_evidence_matrix(self):
        mutations=[lambda r:r.update(overall_ok=1),lambda r:r.update(overall_status='FAIL'),
          lambda r:r.update(run_id='old'),lambda r:r.update(facts=[]),
          lambda r:r['manifest'].update(run_id='old'),lambda r:r['manifest'].update(facts={}),
          lambda r:r['round_e']['audit'].update(cursor_displacement=False),
          lambda r:r['round_e']['audit'].update(ownership_proof={}),
          lambda r:r['round_e']['audit'].update(element_count=0),
          lambda r:r['round_e']['audit'].update(config_copy_post_hash='b'*64),
          lambda r:r['facts'][0].update(hash_valid=False),
          lambda r:r['facts'][0]['fact_json']['payload'].update(taskId='old'),
          lambda r:r['facts'][0]['fact_json'].update(contentHash='sha256:'+'0'*64),
          lambda r:r['facts'][0].update(fact_json=[])]
        for i,mut in enumerate(mutations):
            with self.subTest(case=i):
                r=evidence();mut(r);self.assertFalse(validate_results(r,'test_closeout',True)[0])

    def test_early_round_e_failure_restores_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);cfg=root/'config.json';cfg.write_bytes(b'{}')
            def fail(*args):cfg.write_bytes(b'{"changed":true}');raise RuntimeError('setup failed')
            with patch.object(rr,'_run_round_e_impl',side_effect=fail),self.assertRaises(RuntimeError):
                rr.run_round_e('r',root,root,set(),set(),cfg,'t',None,root)
            self.assertEqual(cfg.read_bytes(),b'{}')

    def test_targeted_missing_facts_fails_and_no_browser_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);cfg=root/'config.json';cfg.write_text('{}')
            with patch.object(rr,'run_round_e',return_value=evidence()['round_e']),patch.object(rr,'harvest_facts',return_value=[]),patch.object(rr,'run_rehearsal_server') as srv,patch.object(rr,'run_interactive_task') as web:
                r=rr.run('test_closeout',root,cfg,target_round='round_e')
            self.assertFalse(r['overall_ok']);srv.assert_not_called();web.assert_not_called()

if __name__=='__main__':unittest.main()
