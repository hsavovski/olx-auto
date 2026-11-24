"""Vision stage, agent loop and drafting, with a scripted LLM and a fake OLX catalog (no network)."""
import itertools
import json
from dataclasses import replace

import pytest
from openai.types.chat import ChatCompletion

from olx_auto import agent, drafting, listings, research, style, validation, vision
from olx_auto.config import Settings
from olx_auto.llm import LLM, LLMError, Reply, parse_json
from tests.conftest import jpeg_bytes

STROLLER_ATTRS = [
    {"code": "state", "label": "Състояние", "validation": {"required": True},
     "values": [{"code": "new", "label": "Ново"}, {"code": "used", "label": "Използвано"}]},
    {"code": "delivery_paid_by", "label": "Доставка",
     "validation": {"required": False, "allow_multiple_values": True},
     "values": [{"code": "207", "label": "Купувач"}, {"code": "209", "label": "Лично"}]},
]
OBSERVATIONS = {"item_type": "baby stroller", "brand": "Chicco", "model": None,
                "condition": "used", "defects": ["scuff on the frame, photo 2"]}
RESEARCH = {"identified_as": "Chicco Liteway 3", "confidence": "high",
            "specs": ["тегло 7.3 кг", "сгъване с една ръка"], "features": [], "variants_note": None,
            "new_price": "199 EUR", "release_year": "2019"}
GOOD_DRAFT = {
    "title": "Детска количка Chicco, сгъваема",
    "description": "Използвана детска количка Chicco. Има леко протриване на рамката, иначе е в добро "
                   "състояние.",
    "category_id": 185, "attributes": {"state": "used", "delivery_paid_by": ["207", "209"]},
    "price": 60, "negotiable": True, "price_reasoning": "Similar strollers sell for 50-80 EUR.",
}


class FakeLLM:
    """Returns scripted replies in order and records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def chat(self, model, messages, **kwargs):
        self.requests.append({"model": model, "messages": [dict(m) for m in messages], **kwargs})
        return self.replies.pop(0)


class FakeCatalog:
    def __init__(self):
        self.queries = []

    def suggest_category(self, q):
        self.queries.append(q)
        return [{"id": 185, "name": "Детски колички", "path": [{"id": 2, "name": "Детски"}]}]

    def category(self, category_id):
        return {"id": category_id, "name": "Детски колички", "is_leaf": category_id != 2,
                "photos_limit": 0 if category_id == 999 else 12}

    def category_attributes(self, category_id):
        return STROLLER_ATTRS


_ids = itertools.count(1)


def text_reply(text, cost=0.001, model="vision-model"):
    return Reply({"role": "assistant", "content": text}, model,
                 {"prompt_tokens": 100, "completion_tokens": 50, "cost": cost})


def research_reply(facts=RESEARCH, cost=0.002):
    return Reply({"role": "assistant", "content": json.dumps(facts, ensure_ascii=False)}, "research-model",
                 {"prompt_tokens": 2000, "completion_tokens": 150, "cost": cost},
                 [{"url": "https://example.com/liteway", "title": "Liteway"}] * 2)


def tool_reply(*calls, cost=0.01):
    tool_calls = [{"id": f"call_{next(_ids)}", "type": "function",
                   "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                  for name, args in calls]
    return Reply({"role": "assistant", "content": "", "tool_calls": tool_calls}, "agent-model",
                 {"prompt_tokens": 1000, "completion_tokens": 200, "cost": cost})


def tool_results(request):
    return [json.loads(m["content"]) for m in request["messages"] if m["role"] == "tool"]


def run(llm, inp=None, **kwargs):
    kwargs = {"catalog": FakeCatalog(), "photo_paths": [], "vision_model": "vision-model", **kwargs}
    return agent.run_agent(llm, "agent-model", inp or agent.AgentInput(observations=OBSERVATIONS), **kwargs)


# ---- llm ------------------------------------------------------------------

def test_llm_chat_reads_openrouter_cost_and_tool_calls(monkeypatch):
    completion = ChatCompletion.model_validate({
        "id": "gen-1", "object": "chat.completion", "created": 0, "model": "anthropic/x",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "suggest_category", "arguments": '{"query": "stroller"}'}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.0123},
    })
    llm = LLM("test-key")
    sent = {}
    monkeypatch.setattr(llm.client.chat.completions, "create",
                        lambda **kw: sent.update(kw) or completion)
    reply = llm.chat("anthropic/x", [{"role": "user", "content": "hi"}], tools=agent.TOOLS)
    assert reply.cost == 0.0123
    assert reply.usage == {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0123}
    assert reply.tool_calls[0]["function"]["name"] == "suggest_category"
    assert reply.message["content"] == ""
    assert sent["extra_body"] == {"usage": {"include": True}}


def test_llm_requires_key():
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        LLM("")


def test_parse_json_tolerates_fences():
    assert parse_json('Here you go:\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json("no json here")


# ---- vision ---------------------------------------------------------------

def test_observe_sends_photos_and_note(tmp_path):
    paths = []
    for i in (1, 2):
        paths.append(tmp_path / f"0{i}.jpg")
        paths[-1].write_bytes(jpeg_bytes())
    llm = FakeLLM(text_reply('```json\n{"item_type": "stroller"}\n```'))
    observations, reply = vision.observe(llm, "vision-model", paths, "Chicco, 2 години")
    assert observations == {"item_type": "stroller"}
    request = llm.requests[0]
    assert request["response_format"] == {"type": "json_object"}
    parts = request["messages"][1]["content"]
    images = [p for p in parts if p["type"] == "image_url"]
    assert len(images) == 2
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "Chicco, 2 години" in parts[-1]["text"]


# ---- research ---------------------------------------------------------------

def test_llm_chat_merges_extra_body_and_reads_citations(monkeypatch):
    completion = ChatCompletion.model_validate({
        "id": "gen-2", "object": "chat.completion", "created": 0, "model": "google/x",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "{}", "annotations": [{
                "type": "url_citation", "url_citation": {"url": "https://example.com/a", "title": "A",
                                                         "start_index": 0, "end_index": 1}}]}}],
    })
    llm = LLM("test-key")
    sent = {}
    monkeypatch.setattr(llm.client.chat.completions, "create",
                        lambda **kw: sent.update(kw) or completion)
    reply = llm.chat("google/x", [], extra_body={"plugins": [{"id": "web"}]})
    assert sent["extra_body"] == {"usage": {"include": True}, "plugins": [{"id": "web"}]}
    assert reply.citations == [{"url": "https://example.com/a", "title": "A"}]


def test_research_searches_the_web_and_keeps_web_links_once():
    reply = research_reply()
    reply.citations.append({"url": "javascript:alert(1)", "title": "x"})
    llm = FakeLLM(reply)
    facts, _ = research.research(llm, "research-model", OBSERVATIONS, "Chicco",
                                 [{"question": "Модел?", "answer": "Liteway 3"}], ["по-евтино"])
    assert facts == {**RESEARCH, "sources": [{"url": "https://example.com/liteway", "title": "Liteway"}]}
    request = llm.requests[0]
    assert request["extra_body"] == {"plugins": [{"id": "web", "max_results": 5}]}
    assert request["response_format"] == {"type": "json_object"}
    prompt = request["messages"][1]["content"]
    assert '"brand": "Chicco"' in prompt and "Liteway 3" in prompt and "по-евтино" in prompt


def test_research_uses_the_configured_search_engine():
    llm = FakeLLM(research_reply())
    research.research(llm, "research-model", OBSERVATIONS, "", engine="exa")
    assert llm.requests[0]["extra_body"] == {"plugins": [{"id": "web", "max_results": 5, "engine": "exa"}]}


def test_search_engine_setting_is_checked(tmp_path, monkeypatch):
    monkeypatch.delenv("SEARCH_ENGINE", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    env = tmp_path / ".env"
    env.write_text("SEARCH_ENGINE=Exa\n")
    assert Settings.load(env).search_engine == "exa"
    env.write_text("SEARCH_ENGINE=google\n")
    with pytest.raises(ValueError, match="SEARCH_ENGINE"):
        Settings.load(env)


def test_research_reruns_only_when_the_seller_says_more(ai_settings, user):
    listing = listings.create(user, "", [jpeg_bytes()])
    listing.update(observations=OBSERVATIONS)
    listings.save(user, listing)
    llm = FakeLLM(research_reply(), tool_reply(("submit_draft", GOOD_DRAFT)))
    drafting.generate(ai_settings, user, listing["id"], llm=llm, catalog=FakeCatalog())

    llm = FakeLLM(tool_reply(("submit_draft", GOOD_DRAFT)))  # nothing new: no research call
    drafting.generate(ai_settings, user, listing["id"], llm=llm, catalog=FakeCatalog())
    assert [r["model"] for r in llm.requests] == ["agent-model"]
    assert "тегло 7.3 кг" in llm.requests[0]["messages"][1]["content"]  # earlier facts reused

    listing = listings.load(user, listing["id"])
    listings.add_message(listing, "Всъщност е Liteway 4")
    listings.save(user, listing)
    llm = FakeLLM(research_reply({**RESEARCH, "identified_as": "Chicco Liteway 4"}, cost=0.003),
                  tool_reply(("submit_draft", GOOD_DRAFT)), learn_reply(changed=False))
    out = drafting.generate(ai_settings, user, listing["id"], llm=llm, catalog=FakeCatalog())
    assert "Liteway 4" in llm.requests[0]["messages"][1]["content"]
    assert out["research"]["identified_as"] == "Chicco Liteway 4"
    assert out["research_seen"] == {"messages": 1, "answers": 0}
    assert out["cost"]["research"] == pytest.approx(0.005)


def test_failed_research_is_traced_and_drafting_goes_on(ai_settings, user):
    listing = listings.create(user, "", [jpeg_bytes()])
    listing.update(observations=OBSERVATIONS)
    listings.save(user, listing)

    class NoWeb(FakeLLM):
        def chat(self, model, messages, **kwargs):
            if model == "research-model":
                raise LLMError("research-model: 503")
            return super().chat(model, messages, **kwargs)

    llm = NoWeb(tool_reply(("submit_draft", GOOD_DRAFT)))
    out = drafting.generate(ai_settings, user, listing["id"], llm=llm, catalog=FakeCatalog())
    assert out["draft"] == GOOD_DRAFT and out["ai_error"] is None
    assert out["research"] is None and out["research_seen"] is None  # tried again next run
    trace = json.loads(listings.trace_path(user, listing["id"]).read_text())
    assert trace["runs"][0] == {"stage": "research", "model": "research-model", "engine": "",
                                "at": trace["runs"][0]["at"], "error": "research-model: 503"}
    assert "Product information" not in agent.build_context(agent.AgentInput(observations=OBSERVATIONS))


# ---- validation -------------------------------------------------------------

def test_good_draft_passes():
    assert validation.check_draft(GOOD_DRAFT, STROLLER_ATTRS) == []


@pytest.mark.parametrize("text, problem", [
    ("Обадете се на 0888 123 456 за въпроси", "phone"),
    ("Пишете на ivan@example.com за въпроси", "email"),
    ("Вижте повече на www.example.bg ако искате", "link"),
    ("СТРАХОТНА КОЛИЧКА В ОТЛИЧНО състояние", "capitals"),
    ("Страхотна количка!!! Побързайте", "repeated"),
])
def test_content_rules(text, problem):
    assert any(problem in p for p in validation.content_problems("description", text))


def test_specs_are_not_phone_numbers():
    assert validation.content_problems("title", "Гуми 205/55 R16, 4 броя, DOT 2023") == []


def test_attribute_problems():
    problems = validation.attribute_problems(
        {"delivery_paid_by": ["207", "999"], "colour": "red"}, STROLLER_ATTRS)
    assert "attributes.state: required (Състояние)" in problems
    assert "attributes.delivery_paid_by: '999' is not an allowed value code" in problems
    assert "attributes.colour: not an attribute of this category" in problems
    assert any("single value" in p for p in
               validation.attribute_problems({"state": ["new", "used"]}, STROLLER_ATTRS))


# ---- agent loop --------------------------------------------------------------

def test_agent_happy_path_fixes_problems_and_submits():
    bad = {**GOOD_DRAFT, "attributes": {}, "title": "КОЛИЧКА CHICCO!!!"}
    llm = FakeLLM(
        tool_reply(("suggest_category", {"query": "детска количка"})),
        tool_reply(("get_category_attributes", {"category_id": 185}),
                   ("look_at_photos", {"question": "Is the rain cover included?"})),
        text_reply("Yes, photo 3 shows a rain cover.", cost=0.002),  # look_at_photos → vision model
        tool_reply(("ask_user", {"questions": ["How old is it?", " "]}), ("submit_draft", bad)),
        tool_reply(("submit_draft", GOOD_DRAFT)),
    )

    result = run(llm, max_cost=1.0)

    assert result.stopped == "submitted"
    assert result.draft == GOOD_DRAFT
    assert result.problems == []
    assert result.questions == ["How old is it?"]
    assert result.cost_vision == pytest.approx(0.002)
    assert result.cost_agent == pytest.approx(0.04)
    # the rejected submit told the model what to fix
    rejected = tool_results(llm.requests[-1])[-1]
    assert rejected["accepted"] is False
    assert "attributes.state: required (Състояние)" in rejected["problems"]
    assert any("capitals" in p for p in rejected["problems"])
    # attributes go to the model compactly, with value codes
    assert llm.requests[2]["model"] == "vision-model"
    attrs = tool_results(llm.requests[3])[1]["attributes"]
    assert attrs[0] == {"code": "state", "label": "Състояние", "required": True,
                        "values": {"new": "Ново", "used": "Използвано"}}
    assert [c["stage"] for c in result.trace["calls"]] == ["agent", "agent", "vision", "agent", "agent"]


def test_agent_rejects_non_leaf_and_closed_categories():
    llm = FakeLLM(
        tool_reply(("get_category_attributes", {"category_id": 2}),
                   ("get_category_attributes", {"category_id": 999})),
        tool_reply(("submit_draft", GOOD_DRAFT)),
    )
    run(llm)
    not_leaf, closed = tool_results(llm.requests[1])
    assert "not a leaf" in not_leaf["error"]
    assert "does not accept" in closed["error"]


def test_agent_reports_bad_tool_calls_to_the_model():
    llm = FakeLLM(
        tool_reply(("nope", {}), ("suggest_category", {"wrong": 1})),
        tool_reply(("submit_draft", GOOD_DRAFT)),
    )
    run(llm)
    unknown, bad_args = tool_results(llm.requests[1])
    assert unknown == {"error": "unknown tool nope"}
    assert "bad arguments" in bad_args["error"]


def test_agent_nudges_when_model_answers_without_tools():
    llm = FakeLLM(text_reply("I think it's a stroller.", model="agent-model"),
                  tool_reply(("submit_draft", GOOD_DRAFT)))
    assert run(llm).stopped == "submitted"
    assert "submit_draft" in llm.requests[1]["messages"][-1]["content"]


def test_iteration_cap_forces_submit_on_last_turn():
    llm = FakeLLM(tool_reply(("suggest_category", {"query": "a"})),
                  tool_reply(("suggest_category", {"query": "b"})),
                  tool_reply(("submit_draft", {**GOOD_DRAFT, "price": 0})))
    result = run(llm, max_iterations=3)
    assert [r["tool_choice"] for r in llm.requests] == [
        "auto", "auto", {"type": "function", "function": {"name": "submit_draft"}}]
    assert result.stopped == "max_iterations"
    assert result.draft["price"] == 0  # best draft so far is kept, with its problems
    assert result.problems == ["price: must be a positive number of EUR"]


def test_cost_cap_stops_the_run():
    llm = FakeLLM(tool_reply(("suggest_category", {"query": "a"}), cost=0.3),
                  tool_reply(("suggest_category", {"query": "b"}), cost=0.3),
                  tool_reply(("submit_draft", GOOD_DRAFT)))
    result = run(llm, max_cost=0.5)
    assert result.stopped == "max_cost"
    assert result.draft is None
    assert len(llm.requests) == 2




def test_locked_fields_survive_the_run_and_refinement_context():
    current = {**GOOD_DRAFT, "title": "Моето заглавие за количката", "price": 45}
    inp = agent.AgentInput(observations=OBSERVATIONS, draft=current, locked=["title", "price"],
                           answered=[{"question": "How old?", "answer": "2 години"}],
                           messages=["има дъждобран", "спомени дъждобрана"])
    llm = FakeLLM(tool_reply(("submit_draft", GOOD_DRAFT)))
    result = run(llm, inp)
    assert result.draft["title"] == "Моето заглавие за количката"
    assert result.draft["price"] == 45
    assert result.draft["description"] == GOOD_DRAFT["description"]
    assert result.category["name"] == "Детски колички"
    assert result.category["attributes"] == STROLLER_ATTRS
    context = llm.requests[0]["messages"][1]["content"]
    for expected in ("## Locked fields", "2 години", "- има дъждобран\n- спомени дъждобрана",
                     "## Current draft"):
        assert expected in context


def test_agent_writes_questions_and_reasoning_in_bulgarian():
    tools = {t["function"]["name"]: t["function"] for t in agent.TOOLS}
    assert "Ask in Bulgarian" in tools["ask_user"]["description"]
    assert tools["submit_draft"]["parameters"]["properties"]["price_reasoning"]["description"].endswith(
        "Bulgarian")


# ---- drafting (listing on disk) -------------------------------------------------

@pytest.fixture
def ai_settings(settings):
    return replace(settings, openrouter_api_key="test-key", vision_model="vision-model",
                   research_model="research-model", agent_model="agent-model")


def learn_reply(changed=True, guide=None, summary="Без „Продавам“ в началото на описанието."):
    if not changed:
        return text_reply('{"changed": false}', model="agent-model")
    guide = guide or ("# Ръководство\n\n## Описание\n- Не започвай описанието с „Продавам“.\n"
                      "- Не пиши какво не е известно за вещта.\n")
    return text_reply(json.dumps({"changed": True, "guide": guide, "summary": summary},
                                 ensure_ascii=False), cost=0.004, model="agent-model")


def test_generate_writes_draft_costs_and_trace(ai_settings, user):
    listing = listings.create(user, "Chicco", [jpeg_bytes(), jpeg_bytes()])
    llm = FakeLLM(
        text_reply(json.dumps(OBSERVATIONS), cost=0.003),
        research_reply(),
        tool_reply(("ask_user", {"questions": ["На колко години е?"]}), ("submit_draft", GOOD_DRAFT)),
    )
    out = drafting.generate(ai_settings, user, listing["id"], llm=llm, catalog=FakeCatalog())

    assert len(llm.requests) == 3  # no feedback yet, so no style learning call
    saved = listings.load(user, listing["id"])
    assert saved == out
    assert saved["observations"] == OBSERVATIONS
    assert saved["draft"] == saved["ai_draft"] == GOOD_DRAFT
    assert saved["category"]["id"] == 185 and saved["category"]["attributes"] == STROLLER_ATTRS
    assert saved["questions"] == [{"id": 1, "question": "На колко години е?", "answer": None}]
    assert saved["progress"] is None and saved["ai_error"] is None
    assert saved["research"] == {**RESEARCH, "sources": [{"url": "https://example.com/liteway",
                                                           "title": "Liteway"}]}
    assert saved["research_seen"] == {"messages": 0, "answers": 0}
    assert saved["cost"] == {"vision": pytest.approx(0.003), "research": pytest.approx(0.002),
                             "agent": pytest.approx(0.01), "style": 0.0}
    trace = json.loads(listings.trace_path(user, listing["id"]).read_text())
    assert [r["stage"] for r in trace["runs"]] == ["vision", "research", "agent"]
    assert "base64" not in json.dumps(trace)
    assert llm.requests[1]["model"] == "research-model"
    assert "Chicco" in llm.requests[1]["messages"][1]["content"]  # researches what vision saw
    context = llm.requests[2]["messages"][1]["content"]
    assert "тегло 7.3 кг" in context and "example.com" not in context  # facts, no links
    assert "+359" not in json.dumps(llm.requests[2]["messages"])  # contact stays out of the prompt


def test_refinement_skips_vision_keeps_answers_and_learns_style(ai_settings, user):
    listing = listings.create(user, "", [jpeg_bytes()])
    listing.update(observations=OBSERVATIONS, draft=GOOD_DRAFT, cost={"vision": 0.003, "agent": 0.01},
                   questions=[{"id": 1, "question": "How old?", "answer": "2 години"},
                              {"id": 2, "question": "Box?", "answer": None}])
    listings.add_message(listing, "по-евтино и без „Продавам“ в началото")
    listings.save(user, listing)
    old_guide = user.style_guide_file.read_text()
    llm = FakeLLM(research_reply(),
                  tool_reply(("ask_user", {"questions": ["Цвят?"]}),
                             ("submit_draft", {**GOOD_DRAFT, "price": 55})),
                  learn_reply())

    out = drafting.generate(ai_settings, user, listing["id"], llm=llm, catalog=FakeCatalog())

    assert [r["model"] for r in llm.requests] == ["research-model", "agent-model", "agent-model"]
    assert "2 години" in llm.requests[0]["messages"][1]["content"]  # research sees answers too
    assert "по-евтино" in llm.requests[1]["messages"][1]["content"]
    assert out["draft"]["price"] == 55
    assert out["questions"] == [{"id": 1, "question": "How old?", "answer": "2 години"},
                                {"id": 3, "question": "Цвят?", "answer": None}]
    assert out["cost"]["agent"] == pytest.approx(0.02)
    assert out["cost"]["style"] == pytest.approx(0.004)
    assert out["learned"] == {"messages": 1, "edits": 0}
    assert out["progress"] is None and out["ai_error"] is None
    # the learner saw the message and the complete listing
    learn_input = llm.requests[2]["messages"][1]["content"]
    assert "по-евтино" in learn_input and GOOD_DRAFT["description"] in learn_input
    assert "Не започвай описанието с „Продавам“" in user.style_guide_file.read_text()
    [entry] = style.log(user)
    assert entry["source"] == listing["id"] and entry["summary"].startswith("Без „Продавам“")
    assert (user.style_history_dir / entry["backup"]).read_text() == old_guide


def test_learn_style_only_sends_new_feedback(ai_settings, user):
    listing = listings.create(user, "", [jpeg_bytes()])
    listing["draft"] = dict(GOOD_DRAFT)
    edited = "Детска количка Chicco, използвана две години. Леко протрита."
    listings.edit_draft(listing, {"description": edited})
    listings.edit_draft(listing, {"attributes": {"state": "new"}})  # a fact: not sent
    listings.save(user, listing)
    guide = user.style_guide_file.read_text()

    llm = FakeLLM(learn_reply(changed=False))
    assert drafting.learn_style(ai_settings, user, listing["id"], llm=llm) is None
    feedback = llm.requests[0]["messages"][1]["content"]
    assert "използвана две години" in feedback and '"state"' not in feedback.split("## New feedback")[1]
    assert user.style_guide_file.read_text() == guide and style.log(user) == []

    # nothing new: no call at all
    assert drafting.learn_style(ai_settings, user, listing["id"], llm=FakeLLM()) is None
    assert listings.load(user, listing["id"])["learned"] == {"messages": 0, "edits": 2}


def test_truncated_guide_is_rejected_and_draft_kept(ai_settings, user):
    listing = listings.create(user, "", [jpeg_bytes()])
    listing.update(observations=OBSERVATIONS)
    listings.add_message(listing, "по-кратко")
    listings.save(user, listing)
    guide = user.style_guide_file.read_text()
    llm = FakeLLM(research_reply(), tool_reply(("submit_draft", GOOD_DRAFT)), learn_reply(guide="# Кратко"))

    out = drafting.generate(ai_settings, user, listing["id"], llm=llm, catalog=FakeCatalog())

    assert out["draft"] == GOOD_DRAFT
    assert out["ai_error"].startswith("Style guide update failed")
    assert out["progress"] is None
    assert user.style_guide_file.read_text() == guide


def test_generate_records_errors(ai_settings, user):
    listing = listings.create(user, "", [jpeg_bytes()])

    class Broken:
        def chat(self, *a, **kw):
            raise LLMError("vision-model: 402 insufficient credits")

    out = drafting.generate(ai_settings, user, listing["id"], llm=Broken(), catalog=FakeCatalog())
    assert out["ai_error"] == "vision-model: 402 insufficient credits"
    assert out["progress"] is None


# ---- style guide storage ------------------------------------------------------------

def test_write_and_revert_guide(user):
    original = user.style_guide_file.read_text()
    style.write_guide(user, "# Първа версия\n" + "x" * 50, summary="Ръчна редакция", source="manual")
    style.write_guide(user, "# Втора версия\n" + "y" * 50, summary="Научено", source="20261123-000000-abcd")
    style.write_guide(user, "# Втора версия\n" + "y" * 50, summary="същото", source="manual")  # no-op
    second, first = style.log(user)
    assert (second["summary"], first["summary"]) == ("Научено", "Ръчна редакция")
    assert second["backup"] != first["backup"]

    style.revert(user, first["backup"])
    assert user.style_guide_file.read_text() == original
    assert style.log(user)[0]["source"] == "revert"
    for bad in ("../user.json", "nope.md"):
        with pytest.raises(KeyError):
            style.revert(user, bad)
