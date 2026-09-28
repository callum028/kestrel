from kestrel.brain.routing import Turn, decide_model


def test_haiku_by_default():
    assert decide_model(Turn(text="what's running?")) == "haiku"


def test_sonnet_when_the_turn_asks_for_depth():
    assert decide_model(Turn(text="why did it choose that approach?")) == "sonnet"
    assert decide_model(Turn(text="walk me through the failure")) == "sonnet"
    assert decide_model(Turn(text="what are the tradeoffs here?")) == "sonnet"


def test_sonnet_for_summarising_a_finished_tasks_report():
    assert decide_model(Turn(text="what happened", summarising_task_report=True)) == "sonnet"


def test_sonnet_for_multiple_questions_in_one_turn():
    assert decide_model(Turn(text="is it done? did CI pass?")) == "sonnet"


def test_single_question_stays_haiku():
    assert decide_model(Turn(text="is it done?")) == "haiku"
