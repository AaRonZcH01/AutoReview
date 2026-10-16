import asyncio
import importlib.util
from pathlib import Path
import pytest
from autoreview.v2.provider import Provider
from autoreview.v2.core import MachineSettings, Problem
from autoreview.v2.execution_config import freeze


def catalogue(models=None, complete=True, warning=None):
    return {'account': {'type': 'chatgpt'}, 'models': models or [],
            'model_catalogue': {'complete': complete}, **({'warning': warning} if warning else {})}


MODEL = {'model': 'gpt-6.1-sol', 'inputModalities': ['image', 'text'],
         'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}, {'reasoningEffort': 'high'}]}


def run_validation(tmp_path, responses):
    provider = Provider(MachineSettings(tmp_path/'settings'))
    calls = []
    async def account(profile_id=None, refresh_models=False):
        calls.append(refresh_models)
        return responses.pop(0)
    provider.account = account
    config = freeze(provider.settings.data)
    async def run():
        return await provider.validate(config)
    return run, calls, config


def test_stale_directory_refreshes_once_without_model_substitution(tmp_path):
    run, calls, config = run_validation(tmp_path, [catalogue([]), catalogue([MODEL])])
    result, _ = asyncio.run(run())
    assert result['model'] == config['model'] == 'gpt-6.1-sol'
    assert result['effort'] == 'medium' and result['service_tier'] == 'default'
    assert calls == [False, True]


def test_loaded_directory_does_not_restart_profile(tmp_path):
    run, calls, _ = run_validation(tmp_path, [catalogue([MODEL])])
    asyncio.run(run())
    assert calls == [False]


@pytest.mark.parametrize('response', [catalogue([]), catalogue([MODEL], complete=False), catalogue([MODEL], warning='timeout')])
def test_incomplete_directory_never_becomes_entitlement_denial(tmp_path, response):
    run, calls, _ = run_validation(tmp_path, [response, response])
    with pytest.raises(Problem) as error: asyncio.run(run())
    assert error.value.code == 'MODEL_CATALOGUE_UNAVAILABLE'
    assert calls == [False, True]


def test_absent_model_reports_component_directory_and_keeps_gate(tmp_path):
    response = catalogue([{**MODEL, 'model': 'another-model'}])
    run, calls, _ = run_validation(tmp_path, [response, response])
    with pytest.raises(Problem) as error: asyncio.run(run())
    assert error.value.code == 'MODEL_UNAVAILABLE'
    assert 'gpt-6.1-sol' in error.value.message and '目录' in error.value.message
    assert calls == [False, True]


def test_null_model_uses_official_id(tmp_path):
    run, calls, _ = run_validation(tmp_path, [catalogue([{**MODEL, 'model': None, 'id': 'gpt-6.1-sol'}])])
    assert asyncio.run(run())[0]['model'] == 'gpt-6.1-sol'


def test_distribution_rejects_cli_missing_default_and_checks_reference_effort(tmp_path):
    path = Path(__file__).resolve().parents[2]/'.delivery/check_cli_catalogue.py'
    spec = importlib.util.spec_from_file_location('catalogue_gate', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    defaults = MachineSettings(tmp_path/'settings').data
    with pytest.raises(RuntimeError, match='missing default image model'):
        module.verify([{**MODEL, 'model': 'gpt-6-sol'}], defaults)
    with pytest.raises(RuntimeError, match='missing default reasoning'):
        module.verify([{**MODEL, 'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}]}], defaults)
    checks = module.verify([MODEL], defaults)
    assert [(c['model'], c['effort']) for c in checks] == [('gpt-6.1-sol', 'medium'), ('gpt-6.1-sol', 'high')]
