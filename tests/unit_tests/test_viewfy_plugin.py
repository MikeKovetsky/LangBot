"""The Viewfy plugin (docker/data/plugins/viewfy__viewfy-agent): how a founder's products reach the chat model.

On 2026-10-23 Mike's DM could not find Viewfy, his 41st product: the DM's prompt named eight products as if they
were all, and a product list over the tool-result budget kept 1,500 characters of its summary.
"""

import json
import sys
import uuid
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / 'docker/data/plugins/viewfy__viewfy-agent'
MODULES = ('main', 'tool_call', 'cta', 'i18n', 'voice')
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


def _scout(n_channels: int, n_profiles: int, active: int = 1) -> dict:
    """The scout tool's get as core/agent/tools.py answers it: every channel with its whole brief."""
    channels = [
        {
            'channel': f'ch{i}', 'label': f'Channel {i}', 'active': i < active, 'auto': False, 'daily_cap': 3,
            'standing': 'unknown', 'standing_line': 'line ' * 20, 'post_access': 'none', 'prompt': 'brief ' * 1200,
        }
        for i in range(n_channels)
    ]
    watch = [f'https://www.linkedin.com/in/person-number-{i}/' for i in range(n_profiles)]
    return {
        'summary': f'{n_channels} channels; watching {n_profiles} LinkedIn profiles',
        'data': {
            'channels': channels, 'hunt': 'Alex comments on pricing. ' * 50, 'skip_authors': [],
            'telegram_venues': [], 'linkedin_watch': watch, 'quora_hours': None,
        },
    }


def test_a_scout_get_over_the_budget_keeps_the_whole_watch_list(plugin):
    """Competera, 2026-11-12: 21 channels with briefs answered about 100,000 characters, and asked for "перші 5
    профайлів" the bot had no list to show."""
    _, tool_call = plugin
    out, oversized = tool_call._fit_tool_json('scout', _scout(21, 21))
    assert oversized and len(tool_call._dumps(out)) <= tool_call.TOOL_JSON_MAX
    data = out['data']
    assert data['linkedin_watch'] == _scout(21, 21)['data']['linkedin_watch']
    assert 'linkedin_watch_total' not in data and 'error' not in out
    assert data['hunt'].startswith('Alex comments on pricing.')
    assert all('prompt' not in c and 'standing_line' not in c for c in data['channels'])
    assert len(data['channels']) == 21 and data['briefs_left_out'] and 'channels_off_left_out' not in data


def test_a_scout_get_keeps_every_channel_when_only_the_briefs_were_too_long(plugin):
    _, tool_call = plugin
    out, oversized = tool_call._fit_tool_json('scout', _scout(4, 3, active=2))
    assert oversized and len(tool_call._dumps(out)) <= tool_call.TOOL_JSON_MAX
    assert [c['channel'] for c in out['data']['channels']] == ['ch0', 'ch1', 'ch2', 'ch3']
    assert out['data']['briefs_left_out'] and 'channels_off_left_out' not in out['data']


def test_a_watch_list_too_long_for_one_reply_keeps_its_head_and_its_total(plugin):
    _, tool_call = plugin
    watched = {'summary': 'Added 1', 'data': {'added': ['x'], 'linkedin_watch': _scout(0, 150)['data']['linkedin_watch']}}
    for answer in (_scout(21, 150), watched):
        out, oversized = tool_call._fit_tool_json('scout', answer)
        assert oversized and len(tool_call._dumps(out)) <= tool_call.TOOL_JSON_MAX
        shown = out['data']['linkedin_watch']
        assert out['data']['linkedin_watch_total'] == 150
        if 'channels' in answer['data']:
            # The switched-off channels went first; the one that is on stays, and so does a hunt brief this short.
            assert [c['channel'] for c in out['data']['channels']] == ['ch0']
            assert out['data']['channels_off_left_out'] == 20
            assert out['data']['hunt'] == answer['data']['hunt'] and 'left_out' not in out['data']
        assert 0 < len(shown) < 150 and shown == answer['data']['linkedin_watch'][: len(shown)]
        # The model is told the list is a head, so it never passes it off as the whole list.
        assert out['summary'].startswith(answer['summary'])
        assert f'first {len(shown)} of the 150 watched profiles' in out['summary']
        # The head is as long as fits: one more profile would not.
        more = dict(out['data'], linkedin_watch=answer['data']['linkedin_watch'][: len(shown) + 1])
        assert len(tool_call._dumps({**out, 'data': more})) > tool_call.TOOL_JSON_MAX


def test_a_long_hunt_brief_never_costs_the_watched_profiles(plugin):
    """The hunt brief has no length cap and grows a paragraph per lane. On Competera's answer a brief of 5,300
    characters left the list three profiles, 5,450 an empty list, and 5,500 the generic cut with no list at all."""
    _, tool_call = plugin
    for n in (21, 150):
        for size in range(0, 12001, 250):
            answer = _scout(21, n)
            answer['data']['hunt'] = 'h' * size
            out, _ = tool_call._fit_tool_json('scout', answer)
            data, watch = out['data'], answer['data']['linkedin_watch']
            assert len(tool_call._dumps(out)) <= tool_call.TOOL_JSON_MAX
            shown = data['linkedin_watch']
            assert shown == watch[: len(shown)]
            if n == 21:
                assert shown == watch and 'linkedin_watch_total' not in data
            else:
                assert len(shown) >= 5 and data['linkedin_watch_total'] == 150
            # The hunt brief is there whole or named as left out; an empty field is not named.
            assert data.get('hunt') == answer['data']['hunt'] or 'hunt' in data['left_out']
            assert 'skip_authors' not in data.get('left_out', [])
            assert data['briefs_left_out'] and 'ch0' in [c['channel'] for c in data['channels']]


def test_the_rest_gives_way_when_it_leaves_the_list_no_room(plugin):
    """Every channel on and a summary naming fifty blogs leave no room for one profile beside a hunt brief under half
    the reply, so the rest goes instead of the list."""
    _, tool_call = plugin
    answer = _scout(21, 150, active=21)
    answer['summary'] += '; blogs ' + ', '.join(f'blog{i}.example.com' for i in range(50))
    answer['data']['hunt'] = 'h' * 2900
    out, _ = tool_call._fit_tool_json('scout', answer)
    assert len(tool_call._dumps(out)) <= tool_call.TOOL_JSON_MAX
    data = out['data']
    assert len(data['linkedin_watch']) >= 5 and data['linkedin_watch_total'] == 150
    assert data['left_out'] == ['hunt'] and len(data['channels']) == 21


def test_a_scout_answer_that_fits_goes_through_whole(plugin):
    _, tool_call = plugin
    small = {'summary': 'Added 1', 'data': {'added': ['x'], 'linkedin_watch': ['a', 'b']}}
    assert tool_call._fit_tool_json('scout', small) == (small, False)


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


class _Telegram:
    """The plugin as _send_tool_cta uses it: Telegram's answer to sendMessage, and what was sent."""

    bot_token = 'token'

    def __init__(self, ok: bool):
        self.ok, self.sent = ok, []

    def lang_for(self, telegram_user_id):
        return 'en'

    async def _tg(self, method, body):
        self.sent.append(body)
        return {'ok': self.ok}

    async def ingest(self, **kwargs):
        pass


async def test_a_manual_launch_sends_its_button_and_hands_the_model_no_link_to_paste(plugin):
    """The tool's summary (core/agent/tools.py, _ads_launch) has the model write the link in words. Once the button
    is out, a link in the reply would come back as a second button, so the model reads the answer without it."""
    _, tool_call = plugin
    url = 'https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1&selected_campaign_ids=2'
    raw = json.dumps({
        'summary': (
            'Launches are manual for this product, so a person switches Navi on with one toggle in Ads Manager. '
            f'Reply with that as the one action, the link in words: [Switch it on in Ads Manager]({url})'
        ),
        'data': {'launched': False, 'needs': 'manual', 'name': 'Navi', 'ads_manager': url, 'button_url': url},
    })

    chat = _Telegram(ok=True)
    out = json.loads(await tool_call._send_tool_cta(chat, 'ads', raw, '42', '7'))
    (sent,) = chat.sent
    assert sent['reply_markup'] == {'inline_keyboard': [[{'text': 'Open in Ads Manager', 'url': url, 'style': 'primary'}]]}
    assert sent['text'] == 'Launches are manual here: switch Navi on in Ads Manager with the button below.'
    assert url not in out['summary'] and 'link in words' not in out['summary']
    assert 'switches Navi on. Button sent in Telegram (Open in Ads Manager)' in out['summary']
    assert out['data']['button_sent'] is True

    # Telegram refused the button: the answer stays as the tool gave it, link and all, for the model to write.
    assert await tool_call._send_tool_cta(_Telegram(ok=False), 'ads', raw, '42', '7') == raw


# A speaker's Join button in a group is t.me/<bot>?start=inv_<token>, so anyone in the group can tap it. The DM
# claim sends the sign-in link only when Viewfy says the tapper is the speaker it was minted for (2026-10-26).

SIGNIN = 'https://api.viewfy.ai/api/telegram/agent/invite?c=claim'
CONNECT = 'https://api.viewfy.ai/api/telegram/agent/connect?t=own'


class _Viewfy:
    """The plugin as the invite DM uses it: Viewfy's answer to the claim, and what went out."""

    bot_token = 'token'

    def __init__(self, answer: dict):
        self.answer, self.asked, self.sent, self.ingested, self.connect_offers = answer, [], [], [], []

    def lang_for(self, telegram_user_id):
        return 'en'

    def remember_lang(self, telegram_user_id, lang):
        return lang

    def turn_start(self, chat_id):
        pass

    def set_quote(self, chat_id, quote):
        pass

    async def _request(self, method, path, *, body=None, **kwargs):
        self.asked.append((method, path, body))
        return dict(self.answer)

    async def claim_product_invite(self, telegram_user_id, token, lang=None):
        import main

        return await main.ViewfyAgentPlugin.claim_product_invite(self, telegram_user_id, token, lang)

    async def send_connect_message(self, chat_id, text, url=None, **kwargs):
        self.sent.append((chat_id, text, url, kwargs.get('button_key')))
        return True

    async def start_reply(self, telegram_user_id, lang=None):
        """The tapper's own Connect offer, which /start without an invite gets."""
        self.connect_offers.append(telegram_user_id)
        return 'Link Telegram to Viewfy to continue.', CONNECT

    async def me(self, telegram_user_id):
        return {'linked': False}

    async def ingest(self, **kwargs):
        self.ingested.append(kwargs)


class _Context:
    def __init__(self, text: str, sender: str, chat: str):
        from langbot_plugin.api.entities.builtin.platform import message as platform_message

        chain = platform_message.MessageChain([platform_message.Plain(text=text)])
        self.event = type('Event', (), {'message_chain': chain, 'sender_id': sender, 'launcher_id': chat})()
        self.stopped = False

    def prevent_default(self):
        self.stopped = True

    def prevent_postorder(self):
        pass

    async def reply(self, chain):
        raise AssertionError('the button went out; no plain reply')


def _ingest_module(monkeypatch):
    import importlib.util

    monkeypatch.syspath_prepend(str(PLUGIN))
    spec = importlib.util.spec_from_file_location('viewfy_ingest', PLUGIN / 'components/events/ingest.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_the_invite_claim_hands_the_sign_in_link_to_the_speaker_only(plugin):
    main, _ = plugin
    claim = main.ViewfyAgentPlugin.claim_product_invite

    ok = _Viewfy({'url': SIGNIN, 'product_name': 'DMarket'})
    assert await claim(ok, '777', 'speakerTOKEN', 'en') == ("Here's your invite to DMarket. Tap Join and sign in.", SIGNIN)
    assert ok.asked == [
        ('POST', '/api/telegram/agent/product_invite/claim', {'token': 'speakerTOKEN', 'telegram_user_id': '777'})
    ]
    # A product named after its page title reads by its short name, as the DM's product anchor does.
    titled = _Viewfy({'url': SIGNIN, 'product_name': 'DMarket | Buy and sell CS2 skins', 'product_domain': 'dmarket.com'})
    text, _ = await claim(titled, '777', 'speakerTOKEN', 'ru')
    assert text == 'Твой инвайт в DMarket (dmarket.com). Нажми «Присоединиться» и войди.'

    text, url = await claim(_Viewfy({'_http_status': 403, 'detail': 'invite is for another Telegram user'}), '888', 'speakerTOKEN', 'en')
    assert url is None and text.startswith('This Join button is for another member of the group')
    text, url = await claim(_Viewfy({'_http_status': 410, 'detail': 'invite expired or already used'}), '777', 'speakerTOKEN', 'ua')
    assert url is None and text == 'Цей інвайт уже недійсний: термін минув або ним уже скористались.'
    # A dropped connection is neither a refusal nor an expiry.
    text, url = await claim(_Viewfy({'_http_status': 0, 'detail': 'timed out'}), '777', 'speakerTOKEN', 'en')
    assert url is None and text == "Couldn't create that link. Try again in a minute."


async def test_a_tap_on_someone_elses_join_button_gets_a_refusal_and_no_sign_in(plugin, monkeypatch):
    ingest = _ingest_module(monkeypatch)
    viewfy = _Viewfy({'_http_status': 403, 'detail': 'invite is for another Telegram user'})
    ctx = _Context('/start inv_speakerTOKEN', sender='888', chat='888')

    await ingest._handle_inbound(viewfy, ctx, event_name='PersonMessageReceived', is_group=False)

    assert viewfy.asked[0][2] == {'token': 'speakerTOKEN', 'telegram_user_id': '888'}
    ((chat, text, url, _),) = viewfy.sent
    assert (chat, url) == ('888', None) and 'another member of the group' in text
    assert ctx.stopped and viewfy.connect_offers == []
    assert viewfy.ingested[-1]['meta']['kind'] == 'product_invite_signin'


async def test_the_speakers_tap_gets_the_join_button_in_the_dm(plugin, monkeypatch):
    ingest = _ingest_module(monkeypatch)
    viewfy = _Viewfy({'url': SIGNIN, 'product_name': 'DMarket'})
    ctx = _Context('/start inv_speakerTOKEN', sender='777', chat='777')

    await ingest._handle_inbound(viewfy, ctx, event_name='PersonMessageReceived', is_group=False)

    assert viewfy.sent == [('777', "Here's your invite to DMarket. Tap Join and sign in.", SIGNIN, 'invite_btn')]
    assert viewfy.connect_offers == []


async def test_an_invite_start_typed_in_a_group_never_puts_a_sign_in_link_there(plugin, monkeypatch):
    """Telegram opens t.me/<bot>?start= in the DM. Typed into the group, a claim would post the speaker's own sign-in
    link where anyone can tap it, so in a group /start stays the sender's Connect offer."""
    ingest = _ingest_module(monkeypatch)
    viewfy = _Viewfy({'url': SIGNIN, 'product_name': 'DMarket'})
    ctx = _Context('/start inv_speakerTOKEN', sender='777', chat='-1001')

    await ingest._handle_inbound(viewfy, ctx, event_name='GroupMessageReceived', is_group=True)

    assert [path for _, path, _ in viewfy.asked if path.endswith('/product_invite/claim')] == []
    assert viewfy.connect_offers == ['777']
    assert [url for _, _, url, _ in viewfy.sent] == [CONNECT]
