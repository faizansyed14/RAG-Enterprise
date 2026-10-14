"""The real-file test's fidelity check must be able to fail: right text passes, other text does not."""

from scripts.scale_e2e_real import passage_match

ORIGINAL = """# Visitor policy

All visitors sign in at reception and wear a badge. Escorts are required in the laboratory.

### Upgrade the tool

You can check the version by running:

```bash
echo $TOOL_VERSION
```

If it is too old, install the newest release from the vendor and restart the service afterwards.
"""


def test_the_documents_own_words_match_exactly_even_when_wrapped_or_missing_a_rendered_title():
    chunk = "All visitors sign in at reception and wear a badge.\nEscorts are required in the laboratory."
    assert passage_match(chunk, ORIGINAL) == ("exact", 1.0)
    with_title = "visitor-policy part 2\n" + chunk                      # a renderer put the file name in front
    assert passage_match(with_title, ORIGINAL)[0] == "exact"


def test_text_the_reader_normalised_counts_as_near_not_exact():
    # the reader dropped the code-fence marker and the "$": same words in nearly the same order
    chunk = ("Upgrade the tool You can check the version by running echo TOOL_VERSION If it is too old, "
             "install the newest release from the vendor and restart the service afterwards.")
    tier, share = passage_match(chunk + " bash", ORIGINAL)
    assert tier in ("exact", "near") and share >= 0.7


def test_text_from_another_document_does_not_match():
    other = ("The kubectl completion script depends on bash-completion which you have to install first. "
             "Thus, to be able to use completion on macOS you must upgrade bash to version four or newer.")
    assert passage_match(other, ORIGINAL)[0] == "none"
    assert passage_match("", ORIGINAL)[0] == "none"
    assert passage_match("too short", ORIGINAL)[0] == "none"
    assert passage_match(other, "")[0] == "none"
