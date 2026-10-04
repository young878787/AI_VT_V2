"""錯誤優先、來源裁切、版本與生成 reference 的回歸測試。"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import memory_testset as testset
from tools import chat_test_cli as cli
from tools.memory_test_evidence import check_turn, answer_result
from domain.chat_test_mode import ChatTestMode


def fixture(mode='short_only'):
    mode = ChatTestMode(mode)
    context = dict(stage='chat_context', history=[], summary='', jev_recent_dialogue=[],
                   messages=[dict(role='system',content='system'),dict(role='user',content='query')],
                   injected_memory_ids=[], memory_fragments=[], system_retained_ranges=[])
    record = dict(turn=2, errors=[], reply='任何模型措辭都原樣保留', stream_complete=True,
                  emotion_source='jev', expression='neutral', db_unchanged=True, memory_changes={}, memory_audit=[],
                  trace=[dict(stage='test_mode', mode=mode.value, short_term_enabled=mode.short_term,
                              memory_read_enabled=mode.memory_read, memory_write_enabled=mode.memory_write),
                         dict(stage='jev',error=None),dict(stage='chat'),context])
    if mode.memory_read:
        record['trace'].append(dict(stage='retrieval',candidates=[],projections=[],errors=[]))
    return record, context


class MemoryTestEvidenceTests(unittest.TestCase):
    def test_actual_reply_is_not_semantically_scored(self):
        case=dict(case_group='short_term',case_type='reference')
        step=dict(phase='probe')
        record, _ = fixture()
        check_turn(case,step,record,[],{})
        self.assertTrue(all(c['passed'] for c in record['hard_checks']))
        self.assertEqual(answer_result(record),'任何模型措辭都原樣保留')

    def test_summary_must_survive_trimming_and_assistant_restatement_must_be_absent(self):
        case=dict(case_group='short_term',case_type='summary_recall')
        step=dict(phase='probe',evidence=[dict(source_step=1,source='summary',fragments=['阻尼'])])
        for retained, restated, valid in ((True,False,True),(False,False,False),(True,True,False)):
            record, context = fixture()
            context.update(summary='目標修阻尼', summary_section_start=100,
                           system_retained_ranges=[[100,110]] if retained else [[0,50]])
            if restated:
                context['messages'].insert(1,dict(role='assistant',content='記得要修阻尼'))
            check_turn(case,step,record,[dict(turn=1)],{})
            check=next(c for c in record['hard_checks'] if c['name']=='source_step_1_summary')
            self.assertEqual(check['passed'],valid)
            if not valid:self.assertTrue(answer_result(record).startswith('錯誤：'))

    def test_unrelated_injected_memory_cannot_substitute_source_evidence(self):
        case=dict(case_group='long_term',case_type='semantic_recall')
        step=dict(phase='probe',evidence=[dict(source_step=1,source='db',fragments=['草莓'])])
        source=str(uuid4())
        previous=[dict(turn=1,memory_event_id=source.replace('-',''),memory_source_ids=[source])]
        state=dict(evidence=[dict(memory_id='target',source_id=source)])
        record, context = fixture('memory_probe')
        context['injected_memory_ids']=['unrelated']
        check_turn(case,step,record,previous,state)
        self.assertEqual(record['hard_status']['memory_evidence_status'],'failed')
        context['injected_memory_ids']=['target']
        context['memory_fragments']=[dict(id='target',text='使用者喜歡草莓',complete=True)]
        check_turn(case,step,record,previous,state)
        self.assertEqual(record['hard_status']['memory_evidence_status'],'passed')

    def test_unknown_memory_fails_even_with_unknown_answer_if_candidates_exist(self):
        record,context=fixture('memory_probe')
        record['reply']='不知道你喜歡哪部電影'
        record['trace'][-1]['candidates']=[dict(id='bad',exact_match=False,similarity=.2)]
        check_turn(dict(case_group='long_term',case_type='unknown_memory'),dict(phase='probe'),record,[],{})
        self.assertTrue(answer_result(record).startswith('錯誤：'))
        self.assertEqual(record['hard_status']['memory_evidence_status'],'failed')

    def test_done_without_audit_or_current_user_source_is_not_committed_success(self):
        record,_=fixture('memory_seed');source=str(uuid4())
        record.update(memory_event_id=source, memory_source_ids=[source],memory_route='process',
                      memory_job_status='done',memory_route_finalized=True)
        check_turn(dict(case_group='long_term',case_type='explicit_memory'),
                   dict(phase='memory_setup',memory_expectation='stored'),record,[],{})
        self.assertEqual(record['hard_status']['memory_evidence_status'],'failed')
        record['memory_audit']=[dict(action='CREATE',target_id='item')]
        state=dict(items={'item':dict(status='active')},sources=[dict(id=source,speaker='user')],
                   evidence=[dict(memory_id='item',source_id=source)])
        check_turn(dict(case_group='long_term',case_type='explicit_memory'),
                   dict(phase='memory_setup',memory_expectation='stored'),record,[],state)
        self.assertEqual(record['hard_status']['memory_evidence_status'],'passed')

    def test_versions_require_retained_history_and_no_old_active_version(self):
        record,_=fixture('memory_probe');source=str(uuid4())
        previous=[dict(turn=1,memory_audit=[dict(target_id='old')]),
                  dict(turn=2,memory_event_id=source,memory_source_ids=[source],memory_audit=[dict(action='SUPERSEDE')])]
        step=dict(phase='probe',versions=[dict(old_step=1,new_step=2,old='NVIDIA',new='AMD',allowed_operations=['SUPERSEDE','MERGE'])])
        state=dict(items={'old':dict(status='active'),'new':dict(status='active')},evidence=[dict(memory_id='new',source_id=source)])
        check_turn(dict(case_group='long_term',case_type='memory_update'),step,record,previous,state)
        self.assertEqual(record['hard_status']['memory_evidence_status'],'failed')
        state['items']['old']['status']='superseded'
        check_turn(dict(case_group='long_term',case_type='memory_update'),step,record,previous,state)
        self.assertEqual(record['hard_status']['memory_evidence_status'],'passed')

    def test_report_shows_every_probe_and_error_instead_of_plausible_reply(self):
        cases=json.loads(testset.CORE_PATH.read_text());case=cases[17]
        records=[]
        for i,s in enumerate(case['conversation'],1):
            if s['phase']=='probe':
                r,_=fixture('memory_probe');r.update(case_id=case['case_id'],turn=i,phase='probe',test_mode='memory_probe',
                    expected_result=s['expected_result'],hard_checks=[dict(layer='isolation_status',name='source',actual=False,expected=True,passed=False,reason='來源不存在')])
                records.append(r)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'memory_report.md';path.with_name('cases.json').write_text(json.dumps([case]))
            cli.write_memory_report(records,path,dict(started_at='now',planned_cases=1,completed_case_ids=[]),'failed',None)
            report=path.read_text()
            self.assertIn('case_018 / probe_1',report);self.assertIn('case_018 / probe_2',report)
            self.assertIn('錯誤：來源不存在',report)
            self.assertIn('最終應該答案／對話',report)
            self.assertNotIn('回答審查',report);self.assertNotIn('最終狀態',report)

    def test_generated_reference_cannot_point_to_probe_or_missing_fact(self):
        from backend.tests.test_memory_testset import generated_cases,core_cases
        generated=generated_cases(core_cases())
        specs=[{k:c[k] for k in ('case_id','case_type','base_case','case_group')} for c in generated]
        for reference in (0,999,2):
            bad=copy.deepcopy(generated);bad[0]['conversation'][-1]['evidence'][0]['source_step']=reference
            with self.assertRaises(ValueError):testset.validate_generated(bad,specs)

    def test_update_workflow_allows_its_written_fact_in_both_sources(self):
        record, context = fixture('mixed_read')
        context['messages'].insert(1, dict(role='user', content='現在 JEV 負責'))
        context['memory_fragments'] = [dict(id='item', text='現在 JEV 負責')]
        step = dict(phase='probe', evidence=[dict(source_step=1, source='history', fragments=['JEV'])])
        check_turn(dict(case_group='mixed', case_type='recall_continue_update'), step, record, [dict(turn=1)], {})
        self.assertEqual(record['hard_status']['isolation_status'], 'passed')
        check_turn(dict(case_group='mixed', case_type='temporary_constraint'), step, record, [dict(turn=1)], {})
        self.assertEqual(record['hard_status']['isolation_status'], 'failed')

    def test_job_wait_uses_330_second_deadline_independent_of_chat_timeout(self):
        import asyncio
        from unittest.mock import Mock,AsyncMock,patch
        event=str(uuid4())
        class Socket:
            async def send(self,payload):self.turn=json.loads(payload)['turn_id'];self.index=0
            async def recv(self):
                events=[dict(type='input_accepted',event_id=event),dict(type='emotion_update',state={},source='jev'),
                        dict(type='expression_plan',debug=dict(intentEmotion='neutral')),dict(type='text_stream',content='reply'),dict(type='stream_end')]
                response=events[self.index];self.index+=1
                return json.dumps(dict(**response,turn_id=self.turn))
        store=Mock();store.snapshot.return_value={};store.audit.return_value=[]
        with patch.object(cli,'wait_memory_job',new=AsyncMock(return_value=dict(status='ignored',route='none'))) as wait:
            asyncio.run(cli.run_turn(Socket(),1,'query','Rushia','test_session',store,120))
        wait.assert_awaited_once_with(store,event)
