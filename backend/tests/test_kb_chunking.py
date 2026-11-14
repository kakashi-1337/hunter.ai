from hunterai.kb import chunking


def check(text, kind):
    cs = chunking.chunk(text, kind)
    assert [c.ordinal for c in cs] == list(range(len(cs)))
    for c in cs:
        assert 0 < len(c.text) <= chunking.MAX_CHUNK_CHARS and c.text == c.text.rstrip() and c.text.strip()
        assert c.line_start == text.count("\n", 0, c.char_start) + 1 and c.line_end == text.count("\n", 0, c.char_end - 1) + 1
        assert kind == "csv" or text[c.char_start:c.char_end] == c.text
    covered = {i for c in cs for i in range(c.char_start, c.char_end)}
    assert all(i in covered for i, ch in enumerate(text) if not ch.isspace())             # nothing is lost
    for a, b in zip(cs, cs[1:]):
        assert b.char_start > a.char_start and a.char_end - b.char_start <= chunking.PROFILES[kind].overlap
    return cs


def test_markdown_splits_at_headings():
    cs = check("".join(f"# Section {i}\n\n" + "word " * 140 + "\n\n" for i in range(4)), "markdown")
    assert len(cs) == 4 and all(c.text.startswith(f"# Section {i}") for i, c in enumerate(cs))


def test_markdown_heading_inside_a_fence_is_not_a_boundary():
    text = "# A\n\n" + "word " * 150 + "\n\n```sh\n# not a heading\n" + "echo hi\n" * 60 + "```\n\n# B\n\n" + "word " * 100
    cs = check(text, "markdown")
    assert not any(c.text.startswith("# not a heading") for c in cs) and any(c.text.startswith("# B") for c in cs)


def test_code_splits_at_top_level_definitions():
    cs = check("".join(f"def f{i}():\n" + "    x = 1\n" * 90 + "\n" for i in range(3)), "code")
    assert [c.text.split("(")[0] for c in cs] == ["def f0", "def f1", "def f2"] and (cs[0].line_end, cs[1].line_start) == (91, 93)


def test_long_paragraph_is_split_with_overlap():
    text = " ".join(f"Sentence number {i} ends here." for i in range(400)); cs = check(text, "text")
    assert len(cs) >= 9 and any(a.char_end > b.char_start for a, b in zip(cs, cs[1:]))
    assert all(text[c.char_start - 1] == " " for c in cs[1:])                             # never starts mid-word


def test_paragraph_chunks_do_not_overlap():
    cs = check("\n\n".join(f"para {i} " + "word " * 180 for i in range(5)), "text")
    assert len(cs) == 5 and all(a.char_end <= b.char_start for a, b in zip(cs, cs[1:]))


def test_unbreakable_run_is_cut_at_target():
    assert [len(c.text) for c in check("x" * 5000, "code")] == [1500, 1500, 1500, 500]


def test_sql_splits_on_statements():
    stmt = "INSERT INTO t (a, b) VALUES\n" + ",\n".join(f"  ({i}, 'value number {i}')" for i in range(22)) + ";\n"
    cs = check(stmt * 6, "sql")
    assert len(cs) > 1 and all(c.text.endswith(";") for c in cs)


def test_csv_repeats_the_header():
    text = "id,name\n" + "".join(f"{i},name{i}\n" for i in range(400)); cs = check(text, "csv")
    assert len(cs) > 1 and all(c.text.startswith("id,name\n") for c in cs) and cs[1].text == "id,name\n" + text[cs[1].char_start:cs[1].char_end]


def test_tiny_tail_is_merged():
    cs = check("\n\n".join(["a " * 500, "b " * 598, "tail end"]), "text")
    assert len(cs) == 2 and cs[1].text.endswith("tail end")
    assert chunking.PROFILES["text"].target < len(cs[1].text) <= chunking.MAX_CHUNK_CHARS


def test_empty_whitespace_and_line_numbers():
    assert chunking.chunk("", "text") == [] and chunking.chunk(" \n\t ", "text") == []
    (c,) = check("l1\nl2\nl3\n", "text")
    assert (c.line_start, c.line_end, c.text) == (1, 3, "l1\nl2\nl3")
