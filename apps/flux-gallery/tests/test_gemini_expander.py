from flux_gallery.gemini_expander import parse_expanded_prompts


def test_parse_expanded_prompts_splits_lines_and_trims():
    text = "\n  first prompt  \nsecond prompt\n\nthird prompt\n"
    assert parse_expanded_prompts(text, count=3) == [
        "first prompt",
        "second prompt",
        "third prompt",
    ]


def test_parse_expanded_prompts_truncates_to_requested_count():
    text = "one\ntwo\nthree\nfour"
    assert parse_expanded_prompts(text, count=2) == ["one", "two"]


def test_parse_expanded_prompts_skips_blank_lines():
    text = "one\n\n\ntwo\n"
    assert parse_expanded_prompts(text, count=2) == ["one", "two"]
