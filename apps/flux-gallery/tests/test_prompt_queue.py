from flux_gallery.prompt_queue import PromptQueue


class _FakeExpander:
    def __init__(self, prompts=None, raises=False):
        self._prompts = prompts or []
        self._raises = raises
        self.calls = 0

    def expand(self, meta_prompt, count):
        self.calls += 1
        if self._raises:
            raise RuntimeError("simulated Gemini failure")
        return list(self._prompts)


def test_refill_triggers_on_first_pop_from_an_empty_queue():
    expander = _FakeExpander(prompts=["a", "b", "c"])
    queue = PromptQueue("meta", queue_size=3, refill_when_below=2, expander=expander)

    assert queue.pop() == "a"
    assert expander.calls == 1


def test_refill_does_not_trigger_while_queue_is_above_threshold():
    expander = _FakeExpander(prompts=["a", "b", "c", "d", "e"])
    queue = PromptQueue("meta", queue_size=5, refill_when_below=2, expander=expander)

    queue.pop()  # 0 <= 2 -> refill to 5, pop "a" -> 4 left
    assert expander.calls == 1
    queue.pop()  # 4 left before this pop, 4 > 2 -> no refill
    assert expander.calls == 1


def test_refill_triggers_again_once_queue_drops_to_threshold():
    expander = _FakeExpander(prompts=["a", "b"])
    queue = PromptQueue("meta", queue_size=2, refill_when_below=1, expander=expander)

    queue.pop()  # 0 <= 1 -> refill to ["a","b"], pop "a" -> ["b"] left
    assert expander.calls == 1
    queue.pop()  # 1 left <= 1 -> refill again before popping
    assert expander.calls == 2


def test_fallback_to_meta_prompt_when_expander_raises():
    expander = _FakeExpander(raises=True)
    queue = PromptQueue(
        "the meta prompt text", queue_size=3, refill_when_below=2, expander=expander
    )

    assert queue.pop() == "the meta prompt text"
    assert expander.calls == 1
