"""The Viewfy plugin (docker/data/plugins/viewfy__viewfy-agent): how a founder's products reach the chat model.

On 2026-10-23 Mike's DM could not find Viewfy, his 41st product: the DM's prompt named eight products as if they
were all, and a product list over the tool-result budget kept 1,500 characters of its summary.
"""

import sys
import uuid
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / 'docker/data/plugins/viewfy__viewfy-agent'
MODULES = ('main', 'tool_call', 'cta', 'i18n')
VIEWFY = {'name': 'Viewfy | He gets you users while you ship', 'domain': 'viewfy.ai'}


@pytest.fixture
def plugin(monkeypatch):
    """The plugin's own modules, imported from its directory, and gone again after the test."""
    monkeypatch.syspath_prepend(str(PLUGIN))
    for name in MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    import main
    import tool_call

    yield main, tool_call
    for name in MODULES:
        sys.modules.pop(name, None)


def _rows(n: int) -> list[dict]:
    """A founder's products as Viewfy lists them (core/agent/tools.py, products): newest first, page-title names."""
    rows = [
        {'id': str(uuid.uuid4()), 'name': f'Product {i} | A tagline as long as a page title', 'domain': f'product{i}.com'}
        for i in range(n - 1)
    ]
    return rows + [{'id': str(uuid.uuid4()), **VIEWFY}]


def _answer(rows: list[dict]) -> dict:
    """The products tool's answer, its summary worded as core/agent/summarize.py products_list words it."""
    named = '; '.join(f'{r["name"]} ({r["domain"]})' for r in rows)
    return {'summary': f'{len(rows)} product(s): {named}', 'data': {'products': rows}}


def test_a_product_list_too_long_for_one_reply_keeps_every_name(plugin):
    _, tool_call = plugin
    out, oversized = tool_call._fit_tool_json('products', _answer(_rows(41)))
    assert oversized and len(tool_call._dumps(out)) <= tool_call.TOOL_JSON_MAX
    assert 'Viewfy | He gets you users while you ship (viewfy.ai)' in out['summary']
    assert 'Product 0 | ' in out['summary'] and 'error' not in out


def test_a_product_list_that_fits_goes_through_whole(plugin):
    _, tool_call = plugin
    small = _answer(_rows(3))
    assert tool_call._fit_tool_json('products', small) == (small, False)


async def test_a_dm_prompt_naming_some_products_says_there_are_more(plugin):
    main, _ = plugin

    class Founder:
        async def products_of(self, telegram_user_id):
            return _rows(41)

    line = await main.ViewfyAgentPlugin.product_anchor(Founder(), '1')
    assert 'Product 0 (product0.com)' in line and 'and 33 more.' in line
    assert 'Pass the name or domain as product_id' in line


def test_a_manual_launch_comes_with_its_ads_manager_button_and_other_ads_links_do_not(plugin):
    import cta

    url = 'https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1&selected_campaign_ids=2'
    launch = {'data': {'launched': False, 'needs': 'manual', 'ads_manager': url, 'button_url': url}}
    assert cta.cta_from_tool('ads', launch) == ('ads', url)
    # A created campaign or a status read carries its link too, but asks nothing of the user yet.
    assert cta.cta_from_tool('ads', {'data': {'ads_manager': url, 'url': url}}) is None
    assert cta.label_for_tool('ads', launch['data'], 'en') == 'Open in Ads Manager'
