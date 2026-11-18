"""State-machine tests: no daily data, no HTTP provider and no credentials."""
from copy import deepcopy
import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from backend.review_engine import ReviewMaterialsChanged, ReviewOutputError
from backend.review_graph import build_review_graph, ReviewStale, ReviewStopped


class Controls:
    stopped = False

    def should_stop(self, task_id):
        return self.stopped


class Engine:
    def __init__(self, kinds=('numeric','functional','document')):
        self.kinds = kinds
        self.calls = []
        self.version = 'v1'
        self.fail_large = False
        self.fail_item = None
        self.controls = None
        self.stop_after_judge = False
        self.uncertain = False
        self.no_requirements = False

    def prepare(self, payload):
        return {'report': {'requirements': [], 'results': [], 'issues': [], 'baselineDocs': [], 'targetDocs': [],
                           'model': 'fixture', 'kb': 'fixture', 'mode': 'three-stage', 'scope': 'dataset'},
                'material_signature': self.version, 'model_signature': 'model-v1',
                'batches': [{'key':'batch','document_id':'source','row_start':0,'row_end':2,'depth':0}]}

    def assert_materials(self, report, material_signature, model_signature):
        if material_signature != self.version:
            raise ReviewMaterialsChanged('changed')

    def extract_batch(self, report, batch):
        self.calls.append(('extract',batch['key']))
        if self.fail_large and not batch['depth']:
            raise ReviewOutputError()
        requirements = [] if self.no_requirements else [
            {'id':'R'+str(i+1).zfill(3),'type':kind,'category':{'numeric':'数值指标','functional':'功能条件','document':'文档存在'}[kind],
             'title':'合成指标'+str(i+1),'criterion':'合成技术要求','question':'合成问句','quote':'合成真实原文','source':{'document_id':'source'}}
            for i,kind in enumerate(self.kinds)]
        return {'requirements':requirements,'filtered':[],'issues':[],'rejected':0}

    @staticmethod
    def split_batch(batch):
        return [{**batch,'key':batch['key']+'/'+str(i),'row_start':i,'row_end':i+1,'depth':1} for i in range(2)]

    @staticmethod
    def merge_requirements(existing, new):
        values = {item['id']:item for item in existing}
        values.update({item['id']:item for item in new})
        return list(values.values())

    @staticmethod
    def confirm_requirements(report, submitted):
        assert submitted
        return deepcopy(submitted)

    def retrieve(self, report, requirement, round_index, previous_chunks, gaps):
        self.calls.append(('retrieve',requirement['id'],round_index))
        return [{'reviewId':'E1','document_id':'plan','document_name':'合成方案.txt','content':'合成设计原文'}]

    def judge(self, report, requirement, chunks):
        identifier = requirement['id']
        self.calls.append(('judge',identifier))
        if self.fail_item == identifier:
            raise ReviewOutputError()
        if self.stop_after_judge:
            self.controls.stopped = True
            self.stop_after_judge = False
        return {'status':'证据不足' if self.uncertain else '已满足','project_plan':'合成方案','analysis':'合成对比',
                'gaps':'合成缺口','recommendation':'人工复核','evidence':[]}

    def check_document(self, report, requirement):
        self.calls.append(('document',requirement['id']))
        return {'status':'已满足','analysis':'只确认文件存在','gaps':'未检查内容','recommendation':'核对原件','evidence':[]}

    def polish_batch(self, report, rows):
        self.calls.append(('polish',len(rows)))
        return [{'id':item['id'],'status':item['answer']['status'],'headline':'合成标题','summary':item['answer']['analysis'],'action':'人工复核'} for item in rows]


@pytest.fixture
def persisted(tmp_path):
    connection = sqlite3.connect(tmp_path/'checkpoints.sqlite3', check_same_thread=False)
    saver = SqliteSaver(connection)
    yield saver
    connection.close()


def config(identifier='fixture'):
    return {'configurable':{'thread_id':identifier},'recursion_limit':10000}


def confirm(graph, state, identifier='fixture'):
    return graph.invoke(Command(resume={'requirements':state['report']['requirements']}),config(identifier))


def test_graph_really_interrupts_before_three_condition_branches_and_preserves_report(persisted):
    engine = Engine()
    graph = build_review_graph(engine,persisted)
    paused = graph.invoke({'task_id':'fixture','request':{}},config())
    assert paused['__interrupt__'][0].value['type'] == 'confirm_requirements'
    assert not any(call[0] in ('retrieve','judge','document') for call in engine.calls)
    completed = confirm(graph,paused)
    assert completed['report']['complete']
    assert len(completed['report']['results']) == 3
    assert ('judge','R001') in engine.calls and ('judge','R002') in engine.calls and ('document','R003') in engine.calls
    structure = graph.get_graph(xray=True).to_json()
    assert len(structure['nodes']) > 20
    assert 'numeric_retrieve' in graph.get_graph(xray=True).draw_mermaid()


def test_failed_large_batch_splits_once_and_successful_batches_do_not_repeat(persisted):
    engine = Engine();engine.fail_large=True
    graph = build_review_graph(engine,persisted)
    paused = graph.invoke({'task_id':'fixture','request':{}},config())
    assert [call[1] for call in engine.calls if call[0]=='extract'] == ['batch','batch/0','batch/1']
    assert len(paused['report']['requirements']) == 3
    confirm(graph,paused)
    assert len([call for call in engine.calls if call[0]=='extract']) == 3


def test_uncertain_items_use_at_most_two_retrieval_rounds_and_are_not_failed_requirements(persisted):
    engine = Engine(('numeric',));engine.uncertain=True
    graph = build_review_graph(engine,persisted)
    done = confirm(graph,graph.invoke({'task_id':'fixture','request':{}},config()))
    assert [call[2] for call in engine.calls if call[0]=='retrieve'] == [0,1]
    assert len([call for call in engine.calls if call[0]=='judge']) == 1
    assert done['report']['results'][0]['status']=='证据不足'
    assert done['report']['results'][0]['retrievalRounds']==2


def test_retry_resumes_optional_interrupt_and_only_retries_failed_item(persisted):
    engine = Engine(('numeric','functional'));engine.fail_item='R002'
    graph = build_review_graph(engine,persisted)
    failed = confirm(graph,graph.invoke({'task_id':'fixture','request':{}},config()))
    assert failed['report']['complete'] and failed['retry_available']
    assert failed['__interrupt__'][0].value['type']=='retry_optional'
    assert len([call for call in engine.calls if call==('judge','R002')])==2
    engine.fail_item=None
    done=graph.invoke(Command(resume={'retry':True}),config())
    assert done['report']['complete'] and not done['retry_available']
    assert len([call for call in engine.calls if call==('judge','R001')])==1
    assert len([call for call in engine.calls if call==('judge','R002')])==3
    assert len(done['report']['results'])==2


def test_real_sqlite_checkpoint_reopens_at_human_confirmation_without_extracting_again(tmp_path):
    path=tmp_path/'state.sqlite3';engine=Engine()
    first=sqlite3.connect(path,check_same_thread=False)
    graph=build_review_graph(engine,SqliteSaver(first))
    paused=graph.invoke({'task_id':'fixture','request':{}},config())
    first.close()
    second=sqlite3.connect(path,check_same_thread=False)
    try:
        restored=build_review_graph(engine,SqliteSaver(second))
        done=confirm(restored,paused)
        assert done['report']['complete']
        assert len([call for call in engine.calls if call[0]=='extract'])==1
    finally:
        second.close()


def test_stop_after_model_output_resumes_checkpoint_without_repeating_that_model_call(persisted):
    engine=Engine(('numeric',));controls=Controls();engine.controls=controls;engine.stop_after_judge=True
    graph=build_review_graph(engine,persisted,controls)
    paused=graph.invoke({'task_id':'fixture','request':{}},config())
    with pytest.raises(ReviewStopped):
        confirm(graph,paused)
    controls.stopped=False
    done=graph.invoke(None,config())
    assert done['report']['complete'] and len(done['report']['results'])==1
    assert len([call for call in engine.calls if call[0]=='judge'])==1


def test_material_change_prevents_resume_from_using_old_conclusions(persisted):
    engine=Engine();graph=build_review_graph(engine,persisted)
    paused=graph.invoke({'task_id':'fixture','request':{}},config());engine.version='v2'
    with pytest.raises(ReviewStale):
        confirm(graph,paused)
    assert not any(call[0]=='judge' for call in engine.calls)


def test_empty_extraction_ends_without_review_or_human_confirmation(persisted):
    engine=Engine();engine.no_requirements=True
    graph=build_review_graph(engine,persisted)
    result=graph.invoke({'task_id':'fixture','request':{}},config())
    assert result['no_requirements'] and result['phase']=='failed'
    assert '__interrupt__' not in result
    assert not any(call[0] in ('judge','document') for call in engine.calls)


def test_changed_materials_are_rejected_at_resume_before_the_next_model_call(persisted):
    engine=Engine(('numeric',));controls=Controls()
    original=engine.retrieve
    def retrieve_then_stop(*args):
        result=original(*args)
        controls.stopped=True
        return result
    engine.retrieve=retrieve_then_stop
    graph=build_review_graph(engine,persisted,controls)
    paused=graph.invoke({'task_id':'fixture','request':{}},config())
    with pytest.raises(ReviewStopped):
        confirm(graph,paused)
    assert not any(call[0]=='judge' for call in engine.calls)
    controls.stopped=False;engine.version='v2'
    with pytest.raises(ReviewStale):
        graph.invoke(None,config())
    assert not any(call[0]=='judge' for call in engine.calls)
