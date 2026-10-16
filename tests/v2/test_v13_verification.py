import copy
import json
from contextlib import contextmanager
from pathlib import Path
from PIL import Image
import pytest
from autoreview.v2.contracts import prompt_record
from autoreview.v2.contracts_v5 import decode_output
from autoreview.v2.grading import grade_observation, resolve_page
from autoreview.v2.scoring import evaluate, matching_source_status
from autoreview.v2.verification import candidates, prepare, merge


def cell(q='q', raw='dad', **change):
    return {'question_id':q,'raw_english':raw,'uncertain':False,'transcription_uncertain':False,
        'visibility':'visible','selected_options':[],'written_options':[],'value':None,'symbol':None,'rank':None,
        'edges':[],'box':None,'unknown_reason':None,'literal_evidence':'clear','ambiguous_glyphs':[],
        'response_evidence':'clear','response_reason':'',**change}


def answer(**change):
    return {'id':'q','number':'1','slot':1,'kind':'fill_blank','response_encoding':'text','english':'dad',**change}


def reread(a, first, second):
    return merge({'items':[a]}, {'cells':[first]}, [{'output':{'cells':[] if second is None else [{**second,'question_id':'q1'}]},
        'attempt_id':'second','manifest':{'targets':[{'question_id':a['id'],'alias':'q1','source_ids':[],
            'number':a['number'],'slot':a['slot'],'kind':a['kind'],'section':'','part_id':''}]}}], 'first')


def test_low_evidence_never_grades_correct_in_any_response_form():
    kinds=[('fill_blank','text'),('choice','written_choice'),('true_false','symbol'),('ordering','rank')]
    for kind,encoding in kinds:
        a=answer(kind=kind,response_encoding=encoding,english='A')
        assert grade_observation(cell(raw='A',symbol='A',rank='A',written_options=['A'],response_evidence='ambiguous'),a)[0]=='unknown'
    a=answer(kind='choice',response_encoding='written_choice',english='A')
    assert grade_observation(cell(raw='A',written_options=['A'],literal_evidence='ambiguous',transcription_uncertain=True),a)[0]=='unknown'
    assert grade_observation(cell(raw='A',written_options=['A'],literal_evidence='not_applicable'),a)[0]=='unknown'
    assert grade_observation(cell(raw='',selected_options=['A'],literal_evidence='ambiguous',transcription_uncertain=True),a)[0]=='correct'
    assert grade_observation(cell(literal_evidence='not_applicable'),answer())[0]=='unknown'


def test_current_contract_all_forms_evidence_and_frozen_decoder():
    for purpose in ('student','student_verify'):
        record=prompt_record(purpose)
        assert record['schema_version']=='6.0'
        properties=record['schema']['$defs']['StudentCellV60']['properties']
        assert properties['response_evidence']['enum']==['clear','ambiguous','not_visible']
        value=cell(response_evidence='ambiguous',response_reason='第二个字母不清楚')
        value.pop('symbol');value.pop('rank')
        decoded=decode_output(purpose,'6.0',json.dumps({'cells':[value],'photo_issues':[]}))
        assert decoded['cells'][0]['uncertain']
    old=cell();old.pop('symbol');old.pop('rank');old.pop('response_evidence');old.pop('response_reason')
    assert not decode_output('student','5.3',json.dumps({'cells':[old],'photo_issues':[]}))['cells'][0]['uncertain']


def test_independent_disagreement_preserves_first_and_prevents_full_credit():
    a=answer();first=cell();output=reread(a,first,cell(raw='dod'))
    assert output['cells']==[first]
    evidence=output['verification']['q']
    assert evidence['first']['raw_english']=='dad' and evidence['second']['raw_english']=='dod'
    assert evidence['status']=='pending'
    result=resolve_page({'items':[a]}, {'slot':1}, output)[0]
    assert result['status']==result['machine_status']=='unknown'
    policy={'declared_total_minor':200,'units':[{'id':'u','question_id':'q','rule':'all_correct','max_points_minor':200}]}
    assert evaluate(policy,[result])['pending_minor']==200
    edited=resolve_page({'items':[a]}, {'slot':1,'overrides':{'q':{'status':'correct'}}},output)[0]
    assert edited['status']=='correct' and evaluate(policy,[edited],reviewed=True)['final_minor']==200


def test_matching_independent_gate_covers_source_units_and_manual_overrides():
    rule={'nodes':[{'id':'s','slot':1,'group':'source'},{'id':'t','slot':1,'group':'target'}],
          'allowed_edges':[['s','t']],'source_group':'source'}
    a=answer(kind='matching',response_encoding='edges',english='',matching_rule=rule)
    first=cell(raw='',literal_evidence='not_applicable',edges=[{'id':'e','from_node':'s','to_node':'t'}])
    targets=candidates({'items':[a]}, {'cells':[first]},1)
    assert targets==[{'question_id':'q','source_ids':['s']}]
    output=merge({'items':[a]}, {'cells':[first]}, [], 'first', [{**targets[0],'reason':'调用失败'}])
    result=resolve_page({'items':[a]}, {'slot':1}, output)[0]
    assert result['status']=='unknown' and matching_source_status(result,'s')=='unknown'
    policy={'declared_total_minor':200,'units':[{'id':'u','question_id':'q','rule':'matching_source','rule_selection':'default','source_id':'s','max_points_minor':200}]}
    assert evaluate(policy,[result])['pending_minor']==200
    result['score_unit_status']={'u':'correct'}
    assert evaluate(policy,[result],reviewed=True)['final_minor']==200


def test_matching_mixed_question_verifies_correct_sources():
    rule={'nodes':[{'id':n,'slot':1,'group':'source' if n.startswith('s') else 'target'} for n in ('s1','s2','t1','t2')],
          'allowed_edges':[['s1','t1'],['s2','t2']],'source_group':'source'}
    a=answer(kind='matching',response_encoding='edges',english='',matching_rule=rule)
    first=cell(raw='',literal_evidence='not_applicable',edges=[{'id':'1','from_node':'s1','to_node':'t1'},{'id':'2','from_node':'s2','to_node':'t1'}])
    targets=candidates({'items':[a]}, {'cells':[first]},1)
    assert targets[0]['source_ids']==['s1']
    output=merge({'items':[a]}, {'cells':[first]}, [], 'first',[{**targets[0],'reason':'复看失败'}])
    result=resolve_page({'items':[a]}, {'slot':1},output)[0]
    assert matching_source_status(result,'s1')=='unknown'
    assert matching_source_status(result,'s2')=='incorrect'


def test_missing_duplicate_or_uncertain_reread_cannot_pass():
    a=answer()
    for second in (None,cell(uncertain=True),cell(response_evidence='ambiguous'),cell(literal_evidence='ambiguous')):
        assert reread(a,cell(),second)['verification']['q']['status']=='pending'
    output=reread(a,cell(),cell())
    assert output['verification']['q']['status']=='verified'
    assert resolve_page({'items':[a]},{'slot':1},output)[0]['status']=='correct'


def test_only_initially_potentially_correct_targets_and_no_invented_confidence():
    ref={'items':[answer(),answer(id='q2',number='2')]}
    output={'cells':[cell(),cell('q2',raw='dod')]}
    assert candidates(ref,output,1)==[{'question_id':'q','source_ids':[]}]
    output['cells'][0]['response_evidence']='ambiguous'
    assert candidates(ref,output,1)==[]


def test_partial_sentence_credit_also_requires_blind_agreement():
    a=answer(kind='sentence',english='I am happy.')
    first=cell(raw='I am happy!')
    ref={'items':[a]}
    policy={'declared_total_minor':200,'units':[{'id':'u','question_id':'q','rule':'sentence_component',
        'component':'word_order_equal','max_points_minor':200}]}
    assert candidates(ref,{'cells':[first]},1,policy)==[{'question_id':'q','source_ids':[]}]
    output=reread(a,first,cell(raw='I happy am!'))
    result=resolve_page(ref,{'slot':1},output)[0]
    assert result['status']=='incorrect'
    assert evaluate(policy,[result])['pending_minor']==200
    output=reread(a,first,cell(raw='I am happy!'))
    result=resolve_page(ref,{'slot':1},output)[0]
    assert result['status']=='incorrect' and evaluate(policy,[result])['earned_minor']==200


class FakeService:
    def __init__(self, root, assets):
        self.store=self;self.root=root;self.assets=assets
    @contextmanager
    def connection(self):
        yield None
    def get(self,db,table,identifier):
        return self.assets[identifier]
    def asset(self,identifier,variant='display'):
        assert variant=='original'
        return self.root/self.assets[identifier]['relative_path']


def test_atlas_native_pixels_no_expected_first_results_and_cross_page_nodes(tmp_path):
    image=Image.new('RGB',(400,500)); image.putdata([(x%256,y%256,(x+y)%256) for y in range(500) for x in range(400)])
    image.save(tmp_path/'original.png')
    identity=[[1,0,0],[0,1,0],[0,0,1]]
    service=FakeService(tmp_path,{'asset':{'relative_path':'original.png','geometry':{'forward':identity,'inverse':identity}},
        'asset2':{'relative_path':'original.png','geometry':{'forward':identity,'inverse':identity}}})
    zone={'x1':.2,'y1':.3,'x2':.6,'y2':.6}
    structure=[{'id':'q','slot':1,'number':'1','kind':'matching','response_encoding':'edges', 'answer_zone':zone,
        'english':'DO_NOT_SEND','allowed_edges':[['s','t']], 'first_status':'correct',
        'nodes':[{'id':'s','slot':1,'group':'a','box':zone},{'id':'t','slot':2,'group':'b','box':zone}]}]
    value=prepare(service,'work',[{'slot':1,'image_version_id':'v','asset_id':'asset'},{'slot':2,'image_version_id':'v2','asset_id':'asset2'}],structure,[{'question_id':'q','source_ids':['s']}])
    assert not value['rejected'] and len(value['batches'])==1
    batch=value['batches'][0];cloud=json.dumps(batch['variables'])
    assert all(k not in cloud for k in ('DO_NOT_SEND','allowed_edges','first_status','source_ids'))
    assert len(batch['manifest']['targets'][0]['tiles'])==2
    for tile in batch['manifest']['targets'][0]['tiles']:
        with Image.open(batch['paths'][tile['atlas_index']-1]) as atlas:
            assert atlas.crop(tile['atlas_pixels']).tobytes()==image.crop(tile['pixels']).tobytes()
    assert batch['manifest']['targets'][0]['tiles'][0]['pixels'][0] < zone['x1']*image.width


def test_missing_cross_page_context_is_explicit_refusal(tmp_path):
    service=FakeService(tmp_path,{})
    structure=[{'id':'q','slot':2,'answer_zone':{'x1':.1,'y1':.1,'x2':.5,'y2':.5}}]
    result=prepare(service,'work',[],structure,[{'question_id':'q','source_ids':[]}])
    assert not result['batches'] and result['rejected'][0]['question_id']=='q'


def test_rotated_source_native_mapping_and_durable_artifact_references(tmp_path):
    im=Image.new('RGB',(80,120));im.putdata([(x*3,y*2,0) for y in range(120) for x in range(80)])
    im.save(tmp_path/'original.png')
    forward=[[0,1,0],[-1,0,1],[0,0,1]];inverse=[[0,-1,1],[1,0,0],[0,0,1]]
    service=FakeService(tmp_path,{'a':{'relative_path':'original.png','geometry':{'forward':forward,'inverse':inverse}}})
    zone={'x1':.2,'y1':.3,'x2':.6,'y2':.6}
    structure=[{'id':'q','slot':1,'number':'1','kind':'fill_blank','answer_zone':zone}]
    batch=prepare(service,'work',[{'slot':1,'asset_id':'a','image_version_id':'v'}],structure,[{'question_id':'q','source_ids':[]}])['batches'][0]
    tile=batch['manifest']['targets'][0]['tiles'][0]
    assert tile['pixels'][0]<32 and tile['pixels'][2]>56
    assert tile['native_rotation_degrees']==90
    with Image.open(batch['paths'][0]) as atlas:
        assert atlas.crop(tile['atlas_pixels']).tobytes()==im.crop(tile['pixels']).transpose(Image.Transpose.ROTATE_90).tobytes()
        mapped=batch['variables']['structure'][0]['answer_zone']
        # The source box (32,24)-(56,72) becomes a 48-by-24 upright region.
        assert round((mapped['x2']-mapped['x1'])*atlas.width)==48
        assert round((mapped['y2']-mapped['y1'])*atlas.height)==24
    artifact=batch['manifest']['targets'][0]['crop_images'][0]
    assert artifact['relative_path'].startswith('attempt-artifacts/work/') and (tmp_path/artifact['relative_path']).is_file()
    assert 'relative_path' not in json.dumps(batch['variables'])


def test_reread_symbol_rank_and_duplicate_response():
    for kind,encoding,raw,value in [('true_false','symbol','√','true'),('ordering','rank','2','2')]:
        a=answer(kind=kind,response_encoding=encoding,english=value)
        first=cell(raw=raw,**{encoding if encoding=='symbol' else 'rank':value})
        second=cell(raw=raw,value=value)
        assert reread(a,first,second)['verification']['q']['status']=='verified'
    a=answer();target={'question_id':'q','alias':'q1','source_ids':[],'number':'1','slot':1,'kind':'fill_blank','section':'','part_id':''}
    output=merge({'items':[a]},{'cells':[cell()]},[{'manifest':{'targets':[target]},'output':{'cells':[cell('q1'),cell('q1')]}}])
    assert output['verification']['q']['status']=='pending' and output['verification']['q']['second'] is None


def test_crop_quality_warning_cannot_be_ignored_by_clear_cells_or_source_units():
    rule={'nodes':[{'id':'s','slot':1,'group':'source'},{'id':'t','slot':1,'group':'target'}],
          'allowed_edges':[['s','t']],'source_group':'source'}
    matching=answer(kind='matching',response_encoding='edges',english='',matching_rule=rule)
    for a, observation, sources in [(answer(),cell(),[]),
            (matching,cell(raw='',literal_evidence='not_applicable',edges=[{'id':'e','from_node':'s','to_node':'t'}]),['s'])]:
        target={'question_id':'q','alias':'q1','source_ids':sources,'number':'1','slot':1,'kind':a['kind'],'section':'','part_id':''}
        output=merge({'items':[a]}, {'cells':[observation]}, [{'manifest':{'targets':[target]},
            'output':{'cells':[{**observation,'question_id':'q1'}],'photo_issues':['q1 的右侧边缘被裁断']}}])
        evidence=output['verification']['q']
        assert evidence['status']=='pending' and evidence['second_photo_issues']==['q1 的右侧边缘被裁断']
        result=resolve_page({'items':[a]}, {'slot':1}, output)[0]
        assert result['status']=='unknown'
        if sources:
            assert matching_source_status(result,'s')=='unknown'


@pytest.mark.parametrize('angle',[0,90,180,270])
def test_native_crop_quarter_turn_has_no_resampling_and_maps_options(tmp_path,angle):
    import numpy as np
    im=Image.new('RGB',(120,160));im.putdata([(x*2,y,0) for y in range(160) for x in range(120)])
    im.save(tmp_path/'original.png')
    matrices={0:[[1,0,0],[0,1,0],[0,0,1]],90:[[0,1,0],[-1,0,1],[0,0,1]],
        180:[[-1,0,1],[0,-1,1],[0,0,1]],270:[[0,-1,1],[1,0,0],[0,0,1]]}
    forward=matrices[angle];inverse=np.linalg.inv(forward).tolist()
    service=FakeService(tmp_path,{'a':{'relative_path':'original.png','geometry':{'forward':forward,'inverse':inverse}}})
    zone={'x1':.2,'y1':.3,'x2':.6,'y2':.6}
    structure=[{'id':'q','slot':1,'number':'1','kind':'choice','answer_zone':zone,
                'options':[{'id':'A','slot':1,'box':zone}]}]
    batch=prepare(service,'work',[{'slot':1,'asset_id':'a','image_version_id':'v'}],structure,[{'question_id':'q','source_ids':[]}])['batches'][0]
    tile=batch['manifest']['targets'][0]['tiles'][0]
    expected=im.crop(tile['pixels'])
    if angle:expected=expected.transpose({90:Image.Transpose.ROTATE_90,180:Image.Transpose.ROTATE_180,270:Image.Transpose.ROTATE_270}[angle])
    assert tile['native_rotation_degrees']==angle
    with Image.open(batch['paths'][0]) as atlas:
        assert atlas.crop(tile['atlas_pixels']).tobytes()==expected.tobytes()
    public=batch['variables']['structure'][0]
    assert public['options'][0]['box']==public['answer_zone']


def test_arbitrary_perspective_keeps_native_pixels_and_small_40_question_batch(tmp_path):
    import numpy as np
    im=Image.new('RGB',(160,120));im.putdata([(x,y,x+y) for y in range(120) for x in range(160)])
    im.save(tmp_path/'original.png')
    forward=[[1,.02,0],[0,1,0],[.01,0,1]]
    service=FakeService(tmp_path,{'a':{'relative_path':'original.png','geometry':{'forward':forward,'inverse':np.linalg.inv(forward).tolist()}}})
    zone={'x1':.2,'y1':.3,'x2':.6,'y2':.6}
    structure=[{'id':str(i),'slot':1,'number':str(i),'kind':'fill_blank','answer_zone':zone} for i in range(40)]
    result=prepare(service,'work',[{'slot':1,'asset_id':'a','image_version_id':'v'}],structure,[{'question_id':str(i),'source_ids':[]} for i in range(40)])
    assert not result['rejected'] and len(result['batches'])==1
    batch=result['batches'][0]
    for target in batch['manifest']['targets']:
        tile=target['tiles'][0]
        assert tile['native_rotation_degrees']==0
        with Image.open(batch['paths'][tile['atlas_index']-1]) as atlas:
            assert atlas.crop(tile['atlas_pixels']).tobytes()==im.crop(tile['pixels']).tobytes()
