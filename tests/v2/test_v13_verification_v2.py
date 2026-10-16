"""Native-pixel batching and conservative channel agreement; no model calls."""
import copy
from types import SimpleNamespace
import pytest
from PIL import Image
from autoreview.v2 import verification as v
from autoreview.v2.core import digest
from test_v13_verification import FakeService, answer, cell


def group(*sizes):
    return {'tiles':[{'image':SimpleNamespace(size=s)} for s in sizes]}


@pytest.mark.parametrize('limit,value,sizes,count,expected',[
    ('MAX_TILE_PIXELS',28_000_000,(2000,2000),8,[7,1]),
    ('MAX_ATLAS_PIXELS',32_000_000,(2000,2000),9,[8,1]),
    ('MAX_ATLAS_IMAGES',6,(2000,2000),7,[6,1]),
    ('MAX_QUESTIONS',40,(10,10),41,[40,1]),
])
def test_each_independent_request_budget_is_enforced(monkeypatch,limit,value,sizes,count,expected):
    for name in ('MAX_TILE_PIXELS','MAX_ATLAS_PIXELS','MAX_ATLAS_IMAGES','MAX_QUESTIONS'):
        monkeypatch.setattr(v,name,10**12)
    monkeypatch.setattr(v,limit,value)
    chunks=v._bounded_chunks([group(sizes) for _ in range(count)])
    assert [len(c) for c in chunks]==expected
    assert all(v._within_budget(v._request_size(c)) for c in chunks)


def test_atlas_whitespace_counts_toward_request_budget(monkeypatch):
    # These widths create a tall mostly empty shelf; raw pixels alone would
    # incorrectly admit the second question under the selected budget.
    groups=[group((2500,100)),group((100,2500))]
    total=v._request_size(groups)
    assert total['tile_pixels']==500_000 and total['atlas_pixels']>6_000_000
    monkeypatch.setattr(v,'MAX_ATLAS_PIXELS',1_000_000)
    assert [len(c) for c in v._bounded_chunks(groups)]==[1,1]


def test_oversized_tile_isolated_without_splitting_cross_page_question():
    special=group((4000,3000),(1000,500))
    chunks=v._bounded_chunks([group((100,100)),special,group((100,100))])
    assert [len(c) for c in chunks]==[1,1,1] and chunks[1][0] is special
    assert v._request_size(chunks[1])['oversized_question_isolated']
    assert len(chunks[1][0]['tiles'])==2


def test_v1_frozen_checkpoint_identity_and_v2_exact_native_pixels(tmp_path):
    image=Image.new('RGB',(2000,1600),(17,63,91));image.save(tmp_path/'original.png')
    identity=[[1,0,0],[0,1,0],[0,0,1]]
    service=FakeService(tmp_path,{'a':{'relative_path':'original.png','geometry':{'forward':identity,'inverse':identity}}})
    structure=[{'id':str(i),'slot':1,'number':str(i),'kind':'fill_blank',
        'answer_zone':{'x1':.1,'y1':.1,'x2':.7,'y2':.7}} for i in range(10)]
    pages=[{'slot':1,'asset_id':'a','image_version_id':'v'}]
    candidates=[{'question_id':str(i),'source_ids':[]} for i in range(10)]
    old=v.prepare(service,'work',pages,structure,candidates,policy_version=v.LEGACY_POLICY_VERSION)
    assert len(old['batches'])==2
    # Frozen before the implementation change, from the original v1 module.
    assert [digest(b['variables']) for b in old['batches']]==[
        'b1a8e38a2cf96ca9ce35c40db9f2fd30f8571e6de48900ead25b39481e5cabdf',
        'f0dbdcc7608cc217ddc5995d53103406b9746b5a4848f76e1d3a8d94174b4dd8']
    assert [[i['sha256'] for i in b['manifest']['images']] for b in old['batches']]==[
        ['938b0450b713e1396fa18d73959921fdf38188c62274a2b33fb18f1f4a8e3cac',
         'cab17e503ec236c0e0e3c042d323bcf742f26c7aefe4b27ed779e79cdac03195'],
        ['938b0450b713e1396fa18d73959921fdf38188c62274a2b33fb18f1f4a8e3cac']]
    latest=v.prepare(service,'new-work',pages,structure,candidates)
    assert len(latest['batches'])==1
    batch=latest['batches'][0];budget=batch['manifest']['request_budget']
    assert budget['atlas_pixels']==sum(i['width']*i['height'] for i in batch['manifest']['images'])
    assert budget['image_count']==len(batch['paths']) and v._within_budget(budget)
    previous={t['question_id']:t for b in old['batches'] for t in b['manifest']['targets']}
    for target in batch['manifest']['targets']:
        tile=target['tiles'][0];prior=previous[target['question_id']]['tiles'][0]
        assert tile['pixels']==prior['pixels'] and tile['native_to_tile']==prior['native_to_tile']
        with Image.open(batch['paths'][tile['atlas_index']-1]) as atlas:
            assert atlas.crop(tile['atlas_pixels']).tobytes()==image.crop(tile['pixels']).tobytes()
        region=next(q for q in batch['variables']['structure'] if q['id']==target['alias'])
        assert region['answer_zone']==v._to_atlas(structure[int(target['question_id'])]['answer_zone'],tile)


def comparison(first,second,policy=v.POLICY_VERSION,**extra):
    a=answer(kind='choice',response_encoding='written_choice',english='D',**extra)
    target={'question_id':'q','alias':'q1','source_ids':[],'number':'1','slot':1,'kind':'choice','section':'','part_id':''}
    result=v.merge({'items':[a]},{'cells':[first]},[{'manifest':{'targets':[target]},
        'output':{'cells':[{**second,'question_id':'q1'}]}}],policy_version=policy)
    return result['verification']['q']


@pytest.mark.parametrize('reverse',[False,True])
def test_redundant_agreeing_choice_channel_passes_only_new_policy(reverse):
    first=cell(raw='D',written_options=['D'])
    second=cell(raw='D',written_options=['D'],selected_options=['D'])
    if reverse:first,second=second,first
    frozen=copy.deepcopy([first,second])
    result=comparison(first,second)
    assert result['status']=='verified' and result['form_differences'] and not result['differences']
    assert comparison(first,second,v.LEGACY_POLICY_VERSION)['status']=='pending'
    assert [first,second]==frozen


@pytest.mark.parametrize('change',[
    {'selected_options':['C']}, {'selected_options':['D','C']},
    {'raw_english':'C'}, {'response_evidence':'ambiguous'}, {'uncertain':True},
    {'transcription_uncertain':True}, {'literal_evidence':'ambiguous'},
    {'ambiguous_glyphs':['D/C']}, {'visibility':'not_visible'},
])
def test_redundant_channel_never_hides_conflict_multiple_or_uncertain(change):
    first=cell(raw='D',written_options=['D'])
    second=cell(raw='D',written_options=['D'],selected_options=['D'])
    second.update(change)
    assert comparison(first,second)['status']=='pending'


def test_no_common_explicit_channel_remains_pending_and_agreement_ignores_answer_key():
    first=cell(raw='',selected_options=['D'],literal_evidence='not_applicable')
    second=cell(raw='D',written_options=['D'])
    assert comparison(first,second)['status']=='pending'
    # Raw transcription can repeat a selected letter in both outputs without
    # proving that either explicit response-form channel was observed twice.
    first=cell(raw='D',selected_options=['D'])
    assert comparison(first,second)['status']=='pending'
    # Agreement itself does not use a reference correct value or repair spelling.
    first=cell(raw='Q',written_options=['Q']);second=cell(raw='Q',written_options=['Q'],selected_options=['Q'])
    a=answer(kind='choice',response_encoding='mixed',english='UNRELATED')
    assert v._agreement(first,second,a,policy_version=v.POLICY_VERSION)[0]
    assert not v._agreement(first,{**second,'raw_english':'q'},a,policy_version=v.POLICY_VERSION)[0]


def test_equal_conflicting_raw_channel_cannot_be_hidden_by_marked_choice():
    first=cell(raw='C',selected_options=['D'])
    assert comparison(first,first)['status']=='pending'
