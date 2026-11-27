"""Pure engine regressions: public streaming never contains model thought text."""
from types import SimpleNamespace
import json

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.runnables import RunnableGenerator, RunnableLambda
import pytest

import backend.collab_graph as collab


@pytest.mark.parametrize('content,expected', [
    ('<think>private reasoning</think>可公开的回答。', '可公开的回答。'),
    ('先说明。<think>private reasoning</think>再说明。', '先说明。再说明。'),
    ('<THINK >private reasoning</THINK >正文', '正文'),
    ('<think>private <think>nested</think> reasoning</think>正文', '正文'),
    ('可见<think>unfinished private reasoning', '可见'),
    ('x < 5 and y > 3; <thinking>普通文字</thinking>', 'x < 5 and y > 3; <thinking>普通文字</thinking>'),
    ('数学表达 x < t', '数学表达 x < t'),
    ('<think' + ' ' * 200 + '>private</think>正文', '正文'),
])
def test_visible_filter_handles_every_single_split_and_character_stream(content, expected):
    for split in range(len(content) + 1):
        visible = collab.VisibleAnswerFilter()
        assert visible.feed(content[:split]) + visible.feed(content[split:]) + visible.finish() == expected
    visible = collab.VisibleAnswerFilter()
    assert ''.join(visible.feed(char) for char in content) + visible.finish() == expected


def answer_engine(chunks):
    def stream(inputs):
        for _ in inputs:
            for chunk in chunks:
                yield AIMessageChunk(content=chunk)
    return collab.CollabEngine(SimpleNamespace(chat=lambda *args, **kwargs: RunnableGenerator(stream)), None)


def answer_state():
    return {'job_id': 'synthetic', 'question': '设备方案说明', 'standalone_question': '设备方案说明', 'intent': 'question',
            'history': [], 'settings': {}, 'references': [{'document_name': '合成.txt', 'content': '方案正文'}]}


def test_sourced_answer_filters_thought_and_waits_for_validation(monkeypatch):
    events = []
    monkeypatch.setattr(collab, '_writer', events.append)
    result = answer_engine(['可见<', 'thi', 'nk>', 'SECRET_PRIVATE', '</thi', 'nk>正文']).answer(answer_state())
    assert result['answer'] == '' and result['pending_answer'] == '可见正文'
    assert not any(event.get('delta') for event in events)
    assert all('SECRET_PRIVATE' not in str(event) for event in events)


def test_stopping_mid_thought_never_persists_private_text(monkeypatch):
    events = []
    monkeypatch.setattr(collab, '_writer', events.append)
    count = 0
    def check_stop(state):
        nonlocal count
        count += 1
        if count == 4:
            raise collab.CollabStopped()
    with pytest.raises(collab.CollabStopped):
        answer_engine(['已公开。<th', 'ink>', 'SECRET_PRIVATE', '尚未结束的思考']).answer(answer_state(), SimpleNamespace(check_stop=check_stop))
    assert not any(event.get('delta') for event in events)
    assert all('SECRET_PRIVATE' not in str(event) for event in events)


def test_reasoning_content_block_is_not_exposed_as_text():
    result = collab._text(SimpleNamespace(content=[{'type': 'reasoning', 'text': 'SECRET_PRIVATE'}, {'type': 'text', 'text': '正文'}]))
    assert result == '正文'


@pytest.mark.parametrize('question', ['设备达标吗？', '请给出本项目结论', '方案符合要求吗', '这些功能满足了么？', 'Can the device pass acceptance?',
    '这份合同有没有问题？', '这样就能验收了吗？', '设备合不合格？', '可以交付吗？', '这个方案没问题吧？'])
def test_common_judgment_phrasings_require_review(question):
    assert collab._review_intent(question)
    state = collab.CollabEngine(None, None).understand({'question': question, 'history': [], 'settings': {}})
    assert state['intent'] == 'review' and state['needs_confirmation']


def test_definition_or_factual_lookup_does_not_force_judgment_review():
    assert not collab._review_intent('什么是合同审查？')
    assert not collab._review_intent('设备续航时间是多少？')


def test_first_question_semantic_intent_is_checked_without_rewriting_its_parameters():
    model = SimpleNamespace(chat=lambda *a, **k: RunnableLambda(lambda p: AIMessage(content=json.dumps({'standalone_question': '模型意外改为99小时', 'intent': 'review'}))))
    state = collab.CollabEngine(model, SimpleNamespace()).understand({'question': '这个项目怎么样？', 'history': [], 'dataset_ids': ['synthetic-kb'], 'settings': {}})
    assert state['standalone_question'] == '这个项目怎么样？'
    assert state['intent'] == 'review' and state['needs_confirmation']


def test_empty_index_avoids_intent_model_call():
    engine = collab.CollabEngine(None, SimpleNamespace(db=SimpleNamespace(one=lambda *args: None)))
    state = engine.understand({'question': '设备续航是多少？', 'history': [], 'dataset_ids': ['empty-kb'], 'settings': {}})
    assert state['standalone_question'] == '设备续航是多少？'


@pytest.mark.parametrize('context_dependent', [True, None])
def test_elliptical_followup_is_understood_even_without_a_pronoun(context_dependent):
    output = {'standalone_question': 'A型设备的实测续航值是多少？', 'intent': 'question'}
    if context_dependent is not None:
        output['context_dependent'] = context_dependent
    model = SimpleNamespace(chat=lambda *a, **k: RunnableLambda(lambda p: AIMessage(content=json.dumps(output))))
    state = collab.CollabEngine(model, SimpleNamespace()).understand({'question': '实测值是多少？', 'history': [{'role': 'user', 'content': '先查A型设备的续航。'}], 'dataset_ids': ['kb'], 'settings': {}})
    assert state['standalone_question'] == 'A型设备的实测续航值是多少？'


def test_clearly_independent_question_with_history_is_not_overridden():
    output = {'standalone_question': 'A型设备的实测续航值是多少？', 'intent': 'question', 'context_dependent': False}
    model = SimpleNamespace(chat=lambda *a, **k: RunnableLambda(lambda p: AIMessage(content=json.dumps(output))))
    state = collab.CollabEngine(model, SimpleNamespace()).understand({'question': 'B型设备的重量是多少？', 'history': [{'role': 'user', 'content': '先查A型设备的续航。'}], 'dataset_ids': ['kb'], 'settings': {}})
    assert state['standalone_question'] == 'B型设备的重量是多少？'


@pytest.mark.parametrize('answer', ['可以验收。', '方案符合要求。', '验收已通过。', '不符合交付要求。', '判定为合格。'])
def test_answer_itself_cannot_sneak_an_unreviewed_verdict(answer):
    assert collab._strong_verdict(answer)


@pytest.mark.parametrize('answer', ['不能确认验收通过。', '尚未证明方案符合要求。', '如果方案符合要求且完成实测，才可以验收。', '请核对是否符合交付要求。'])
def test_gap_or_conditional_text_is_not_a_confirmed_verdict(answer):
    assert not collab._strong_verdict(answer)


@pytest.mark.parametrize("answer,positive", [
    ("该设备满足续航要求[1]。", True),
    ("离线缓存功能已实现[1]。", True),
    ("设备不满足续航要求[1]。", False),
    ("如果实测达标，可以验收。", False),
    ("尚不能确认满足续航要求。", False),
])
def test_pass_check_recognizes_affirmative_wording_when_model_omits_status(answer, positive):
    assert collab._positive_verdict(answer) == positive


def test_model_context_and_reference_views_are_bounded():
    refs = collab._source_input([{'content': 'x' * 12000} for _ in range(40)])
    assert len(refs) == 40 and sum(len(ref['content']) for ref in refs) <= 96000
    history = collab._history({'history': [{'role': 'user' if index % 2 == 0 else 'assistant', 'content': 'x' * 8000} for index in range(25)]})
    assert len(history) <= 20 and sum(len(row['content']) for row in history) <= 24000


def test_collab_keeps_late_measurements_for_all_sixteen_sources():
    review = collab.CollabEngine(None, SimpleNamespace(search=lambda options: {"chunks": [
        {"id": str(index), "document_id": "doc", "dataset_id": "kb",
         "content": "批次A。\n" + "一般介绍。" * 2600 + "\n满载复测：设备续航为6小时。"}
        for index in range(20)
    ][:options["page_size"]]}))
    state = {"dataset_ids": ["kb"], "standalone_question": "设备满载续航是多少？"}
    sources = review.retrieve(state)["references"]
    sent = collab._source_input(sources, question=state["standalone_question"])
    assert len(sent) == 16
    assert all("批次A" in item["content"] and "续航为6小时" in item["content"] for item in sent)
